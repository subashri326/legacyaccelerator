"""Read Oracle and Teradata DDL exports into the catalog.

Real exports carry vendor options a general SQL parser rejects: Oracle's DBMS_METADATA output
(quoted names, ``ENABLE``, ``STORAGE(...)``, ``TABLESPACE``, separate ``ALTER TABLE ... ADD
CONSTRAINT``, ``/`` terminators) and Teradata's ``SHOW TABLE`` output (``MULTISET``,
``FALLBACK``, ``CHARACTER SET``, ``[NOT] CASESPECIFIC``, ``FORMAT``, ``COMPRESS``, ``PRIMARY
INDEX``). This reader splits statements itself, reads table definitions column by column and
keeps the details that change behaviour:

* Teradata ``NOT CASESPECIFIC`` columns compare case-insensitively (the targets don't), so they
  are marked for review wherever they are compared.
* A Teradata ``UNIQUE PRIMARY INDEX`` on NOT NULL columns is used as the row key; a non-unique
  primary index is only a distribution key and is not.
* Oracle ``VARCHAR2(n CHAR)`` / ``NVARCHAR2`` and Teradata ``CHARACTER SET UNICODE`` are
  recorded as Unicode text (it changes the byte length needed on some targets).

Views keep their SQL; procedures, functions, packages, triggers and macros are kept whole.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from accelerator.ir.models import CatalogIR, ColumnIR, ProcedureIR, TableIR, ViewIR

_IDENT = r'(?:"[^"]+"|[A-Za-z_][\w$#]*)'
_QNAME = rf"{_IDENT}(?:\s*\.\s*{_IDENT}){{0,2}}"
_PROC_KINDS = r"PROCEDURE|FUNCTION|PACKAGE(?:\s+BODY)?|TRIGGER|TYPE(?:\s+BODY)?|MACRO"
_CREATE = r"(?:CREATE(?:\s+OR\s+REPLACE)?|REPLACE)"


def _unquote(name: str) -> str:
    return name.strip().strip('"').strip()


def _split_name(qname: str) -> tuple[str, str]:
    parts = [_unquote(p) for p in re.findall(_IDENT, qname)]
    return (parts[-2] if len(parts) >= 2 else ""), parts[-1]


def _mask(text: str) -> str:
    """Blank out string literals and comments (same length) so structure can be scanned."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        if text.startswith("--", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out[i:j] = " " * (j - i)
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out[i:j] = [c if c == "\n" else " " for c in text[i:j]]
            i = j
        elif text[i] == "'":
            j = i + 1
            while j < n:
                if text[j] == "'" and j + 1 < n and text[j + 1] == "'":
                    j += 2
                    continue
                if text[j] == "'":
                    break
                j += 1
            out[i + 1 : j] = " " * (j - i - 1)
            i = j + 1
        else:
            i += 1
    return "".join(out)


def split_statements(text: str, dialect: str) -> list[str]:
    """Split a script into statements, keeping procedural blocks (PL/SQL, SPL, macros) whole."""
    masked = _mask(text)
    # Oracle scripts end PL/SQL blocks with a line holding only "/".
    bounds = [0]
    if dialect == "oracle":
        for m in re.finditer(r"(?m)^[ \t]*/[ \t]*$", masked):
            bounds += [m.start(), m.end()]
    bounds.append(len(text))
    stmts: list[str] = []
    for b in range(0, len(bounds) - 1, 2):
        lo, hi = bounds[b], bounds[b + 1]
        start, depth, i = lo, 0, lo
        while i < hi:
            ch = masked[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == ";" and depth == 0:
                head = masked[start:i]
                if re.match(
                    rf"\s*{_CREATE}\s+(?:EDITIONABLE\s+|NONEDITIONABLE\s+)?"
                    rf"(?:{_PROC_KINDS})\b",
                    head,
                    re.IGNORECASE,
                ):
                    if dialect == "oracle":
                        i = hi  # the block runs to the "/" line
                        break
                    begins = len(re.findall(r"\bBEGIN\b", head, re.IGNORECASE))
                    ends = len(
                        re.findall(
                            r"\bEND\b(?!\s+(?:IF|LOOP|WHILE|CASE|FOR|REPEAT)\b)",
                            head,
                            re.IGNORECASE,
                        )
                    )
                    if begins and begins > ends:
                        i += 1
                        continue
                    stmts.append(text[start : i + 1].strip())
                else:
                    stmts.append(text[start:i].strip())
                start = i + 1
            i += 1
        stmts.append(text[start:hi].strip())
    stmts = [_strip_leading_comments(s) for s in stmts]
    return [
        s.rstrip(";").strip()
        if not re.match(
            rf"\s*{_CREATE}\s+(?:EDITIONABLE\s+|"
            rf"NONEDITIONABLE\s+)?(?:{_PROC_KINDS})\b",
            _mask(s),
            re.IGNORECASE,
        )
        else s
        for s in stmts
        if _mask(s).strip()
    ]


def _strip_leading_comments(stmt: str) -> str:
    masked = _mask(stmt)
    lead = len(masked) - len(masked.lstrip())
    return stmt[lead:].strip()


def _top_level_split(body: str) -> list[str]:
    masked = _mask(body)
    parts, depth, last = [], 0, 0
    for i, ch in enumerate(masked):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(body[last:i].strip())
            last = i + 1
    parts.append(body[last:].strip())
    return [p for p in parts if p]


def _matching_paren(masked: str, open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(masked)):
        if masked[i] == "(":
            depth += 1
        elif masked[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


_TYPE = re.compile(
    r"""^(
        TIMESTAMP\s*(?:\(\s*\d+\s*\))?\s*WITH\s+(?:LOCAL\s+)?TIME\s+ZONE
      | TIME\s*(?:\(\s*\d+\s*\))?\s*WITH\s+TIME\s+ZONE
      | INTERVAL\s+\w+(?:\s*\(\s*\d+\s*\))?(?:\s+TO\s+\w+(?:\s*\(\s*\d+\s*\))?)?
      | LONG\s+RAW | LONG\s+VARCHAR | DOUBLE\s+PRECISION | CHARACTER\s+VARYING
      | CHAR\s+VARYING | NATIONAL\s+CHAR(?:ACTER)?(?:\s+VARYING)? | PERIOD\s*\(\s*\w+(?:\s*\(\s*\d+\s*\))?\s*\)
      | [A-Za-z_][\w$]*(?:\s*\(\s*[^)]*\))?
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def _parse_column(item: str, dialect: str, pos: int) -> ColumnIR | None:
    m = re.match(rf"^\s*({_IDENT})\s+(.*)$", item, re.DOTALL)
    if not m:
        return None
    name, rest = _unquote(m.group(1)), m.group(2).strip()
    tm = _TYPE.match(rest)
    if not tm:
        return None
    dtype = re.sub(r"\s+", " ", tm.group(1)).strip()
    dtype = re.sub(r"\s*\(\s*", "(", dtype)
    dtype = re.sub(r"\s*\)", ")", dtype)
    dtype = re.sub(r"\s*,\s*", ",", dtype)
    attrs_text = rest[tm.end() :]
    masked = _mask(attrs_text).upper()
    attributes: dict[str, object] = {}
    up = dtype.upper()
    char_semantics = " CHAR)" in up or up.startswith(("NVARCHAR2", "NCHAR", "NCLOB", "NATIONAL"))
    if char_semantics or re.search(r"CHARACTER\s+SET\s+UNICODE", masked):
        attributes["unicode"] = True
    dtype = re.sub(
        r"\s+(BYTE|CHAR)\)", ")", dtype, flags=re.IGNORECASE
    )  # VARCHAR2(200 CHAR) -> (200)
    if re.search(r"\bNOT\s+CASESPECIFIC\b", masked):
        attributes["case_insensitive"] = True
    fmt = re.search(r"\bFORMAT\s+'([^']*)'", attrs_text, re.IGNORECASE)
    if fmt:
        attributes["format"] = fmt.group(1)
    default = None
    dm = re.search(r"\bDEFAULT\b", masked)
    if dm:
        tail = attrs_text[dm.end() :].strip()
        stop = re.search(
            r"\s+(?:NOT\s+NULL|NULL|ENABLE|CONSTRAINT|CHECK|PRIMARY|UNIQUE|"
            r"REFERENCES|FORMAT|COMPRESS|TITLE|CASESPECIFIC|NOT\s+CASESPECIFIC)\b",
            _mask(tail),
            re.IGNORECASE,
        )
        default = (tail[: stop.start()] if stop else tail).strip() or None
    nullable = not re.search(r"\bNOT\s+NULL\b", masked)
    is_pk = bool(re.search(r"\bPRIMARY\s+KEY\b", masked))
    params = re.search(r"\(([^)]*)\)", dtype)
    precision = scale = length = None
    if params:
        nums = [p.strip() for p in params.group(1).split(",")]
        base = dtype.split("(")[0].upper()
        if base in ("NUMBER", "NUMERIC", "DECIMAL", "DEC"):
            precision = int(nums[0]) if nums[0].isdigit() else None
            if len(nums) > 1 and re.fullmatch(r"-?\d+", nums[1]):
                scale = int(nums[1])
        elif nums[0].isdigit():
            length = int(nums[0])
    return ColumnIR(
        name=name,
        data_type=dtype,
        length=length,
        precision=precision,
        scale=scale,
        nullable=nullable and not is_pk,
        is_primary_key=is_pk,
        default_expr=default,
        ordinal_position=pos,
        attributes=attributes,
    )


def _cols_in(text: str) -> list[str]:
    return [_unquote(c) for c in re.findall(_IDENT, text)]


def _create_table(stmt: str, dialect: str, database: str) -> TableIR | None:
    masked = _mask(stmt)
    m = re.match(
        r"\s*CREATE\s+(?:GLOBAL\s+TEMPORARY\s+|VOLATILE\s+|PRIVATE\s+TEMPORARY\s+)?"
        r"(?:(?:SET|MULTISET)\s+)?(?:GLOBAL\s+TEMPORARY\s+|VOLATILE\s+)?TABLE\s+"
        rf"({_QNAME})",
        masked,
        re.IGNORECASE,
    )
    if not m:
        return None
    schema, name = _split_name(stmt[m.start(1) : m.end(1)])
    open_idx = masked.find("(", m.end())
    if open_idx < 0:
        return None
    if re.search(r"\bAS\s*$", masked[m.end() : open_idx], re.IGNORECASE):
        return None  # CREATE TABLE ... AS (SELECT ...): no explicit columns
    close_idx = _matching_paren(masked, open_idx)
    body = stmt[open_idx + 1 : close_idx]
    trailer = masked[close_idx + 1 :]
    cols: list[ColumnIR] = []
    pk: list[str] = []
    fks: dict[str, str] = {}
    for item in _top_level_split(body):
        mi = _mask(item)
        cm = re.match(
            rf"\s*(?:CONSTRAINT\s+{_IDENT}\s+)?(PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE|CHECK)\b",
            mi,
            re.IGNORECASE,
        )
        if cm:
            kind = cm.group(1).upper()
            inner = re.search(r"\(([^)]*)\)", item[cm.end() :])
            if kind.startswith("PRIMARY") and inner:
                pk = _cols_in(inner.group(1))
            elif kind.startswith("FOREIGN") and inner:
                ref = re.search(rf"REFERENCES\s+({_QNAME})", item, re.IGNORECASE)
                for c in _cols_in(inner.group(1)):
                    fks[c.lower()] = (
                        ".".join(p for p in _split_name(ref.group(1)) if p) if ref else ""
                    )
            continue
        col = _parse_column(item, dialect, len(cols) + 1)
        if col is not None:
            cols.append(col)
    key_source = "PRIMARY KEY" if pk or any(c.is_primary_key for c in cols) else None
    upi = re.search(r"\bUNIQUE\s+PRIMARY\s+INDEX\s*(?:\w+\s*)?\(([^)]*)\)", trailer, re.IGNORECASE)
    if not pk and not key_source and upi:
        upi_cols = {
            c.lower()
            for c in _cols_in(stmt[close_idx + 1 + upi.start(1) : close_idx + 1 + upi.end(1)])
        }
        if upi_cols and all(not c.nullable for c in cols if c.name.lower() in upi_cols):
            pk = [c.name for c in cols if c.name.lower() in upi_cols]
            key_source = "UNIQUE PRIMARY INDEX"
    for c in cols:
        if c.name.lower() in {p.lower() for p in pk}:
            c.is_primary_key, c.nullable = True, False
        if c.name.lower() in fks:
            c.is_foreign_key, c.fk_target = True, fks[c.name.lower()]
    return TableIR(
        database=database,
        schema_name=schema or ("dbo" if dialect == "tsql" else ""),
        name=name,
        columns=cols,
        source_sql=stmt,
        attributes={"key_source": key_source} if key_source else {},
    )


def read_ddl_export(text: str, source_system: str, dialect: str, database: str) -> CatalogIR:
    tables: dict[str, TableIR] = {}
    views: list[ViewIR] = []
    procs: list[ProcedureIR] = []
    # SQL*Plus / BTEQ session commands are not part of the schema.
    text = re.sub(r"(?im)^[ \t]*(?:SET|PROMPT|SPOOL|REM|REMARK|WHENEVER|@|\.)[^\n]*$", "", text)
    for stmt in split_statements(text, dialect):
        masked = _mask(stmt)
        head = masked.lstrip().upper()
        if re.match(
            r"CREATE\s+(?:GLOBAL\s+TEMPORARY\s+|VOLATILE\s+|PRIVATE\s+TEMPORARY\s+)?"
            r"(?:(?:SET|MULTISET)\s+)?(?:GLOBAL\s+TEMPORARY\s+|VOLATILE\s+)?TABLE\b",
            head,
        ):
            t = _create_table(stmt, dialect, database)
            if t is not None:
                tables[f"{t.schema_name}.{t.name}".lower()] = t
            continue
        am = re.match(
            rf"\s*ALTER\s+TABLE\s+({_QNAME})\s+ADD\s+(?:CONSTRAINT\s+{_IDENT}\s+)?"
            r"(PRIMARY\s+KEY|FOREIGN\s+KEY)\s*\(([^)]*)\)",
            masked,
            re.IGNORECASE,
        )
        if am:
            schema, name = _split_name(stmt[am.start(1) : am.end(1)])
            t = tables.get(f"{schema}.{name}".lower())
            cols = {c.lower() for c in _cols_in(stmt[am.start(3) : am.end(3)])}
            if t is not None:
                if am.group(2).upper().startswith("PRIMARY"):
                    for c in t.columns:
                        if c.name.lower() in cols:
                            c.is_primary_key, c.nullable = True, False
                    t.attributes["key_source"] = "PRIMARY KEY"
                else:
                    ref = re.search(rf"REFERENCES\s+({_QNAME})", stmt, re.IGNORECASE)
                    target = ".".join(p for p in _split_name(ref.group(1)) if p) if ref else ""
                    for c in t.columns:
                        if c.name.lower() in cols:
                            c.is_foreign_key, c.fk_target = True, target
            continue
        vm = re.match(
            rf"\s*{_CREATE}\s+(?:FORCE\s+|NOFORCE\s+|EDITIONABLE\s+|NONEDITIONABLE\s+)*"
            rf"(?:RECURSIVE\s+)?VIEW\s+({_QNAME})(\s*\([^)]*\))?\s+AS\b",
            masked,
            re.IGNORECASE,
        )
        if vm:
            schema, name = _split_name(stmt[vm.start(1) : vm.end(1)])
            select = stmt[vm.end() :].strip()
            select = re.sub(r"^\s*LOCKING\s+ROW\s+FOR\s+ACCESS\s+", "", select, flags=re.IGNORECASE)
            select = re.sub(r"\s+WITH\s+(?:READ\s+ONLY|CHECK\s+OPTION)(?:\s+CONSTRAINT\s+\S+)?\s*$",
                            "", select, flags=re.IGNORECASE)  # fmt: skip
            refs: list[str] = []
            try:
                tree = sqlglot.parse_one(select, read=dialect)
                refs = sorted(
                    {".".join(p for p in (t.db, t.name) if p) for t in tree.find_all(exp.Table)}
                )
            except (SqlglotError, ValueError):
                pass
            col_list = stmt[vm.start(2) : vm.end(2)] if vm.group(2) else ""
            qualified = ".".join(p for p in (schema, name) if p)
            views.append(
                ViewIR(
                    database=database,
                    schema_name=schema,
                    name=name,
                    definition_sql=f"CREATE VIEW {qualified}{col_list} AS {select}",
                    referenced_tables=refs,
                )
            )
            continue
        pm = re.match(
            rf"\s*{_CREATE}\s+(?:EDITIONABLE\s+|NONEDITIONABLE\s+)?({_PROC_KINDS})\s+"
            rf"({_QNAME})",
            masked,
            re.IGNORECASE,
        )
        if pm:
            schema, name = _split_name(stmt[pm.start(2) : pm.end(2)])
            procs.append(ProcedureIR(
                database=database, schema_name=schema, name=name, source_dialect=dialect,
                definition_sql=stmt,
                referenced_tables=sorted(set(re.findall(rf"\b(?:FROM|JOIN)\s+({_QNAME})", stmt,
                                                        re.IGNORECASE))),
                target_tables=sorted(set(re.findall(
                    rf"\b(?:INSERT\s+INTO|INS\s+INTO|UPDATE|UPD|MERGE\s+INTO|DELETE\s+FROM|DEL\s+FROM)"
                    rf"\s+({_QNAME})", stmt, re.IGNORECASE))),
                has_dynamic_sql=bool(re.search(r"EXECUTE\s+IMMEDIATE|DBMS_SQL|CALL\s+DBC\.SYSEXECSQL",
                                               stmt, re.IGNORECASE)),
                has_cursors=bool(re.search(r"\bCURSOR\b|\bFOR\s+\w+\s+IN\s*\(", stmt, re.IGNORECASE)),
            ))  # fmt: skip
            continue
        # Indexes, grants, comments, sequences, synonyms, statistics: not part of the catalog.
    return CatalogIR(
        source_system=source_system,
        database_name=database,
        tables=list(tables.values()),
        views=views,
        procedures=procs,
    )
