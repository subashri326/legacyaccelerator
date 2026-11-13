"""Rewrite database SQL so it means the same on the target - or flag it.

A straight dialect translation keeps the words but not always the meaning. These rules make the
meaning explicit before the SQL is rendered for the target. Column types come from the catalog,
because several rules depend on them.

All sources
  * ``GROUP BY 1`` (by position) is replaced by the expression (SQL Server/Fabric reject it).
  * Functions the translator does not know are flagged instead of passed through.
SQL Server and Teradata
  * integer / integer drops the remainder there; the targets return a decimal -> integer division.
Oracle
  * ``''`` is NULL; ``||`` ignores NULL operands; ``SUBSTR`` start 0 means 1 and a negative start
    counts from the end; ``DECODE`` matches NULL to NULL; ``date + n`` adds n days;
    ``TRUNC(date)``/``TO_CHAR(date, fmt)`` are date functions; ``(+)`` outer joins become ANSI joins.
  * Flagged: ``ROWNUM``, ``CONNECT BY``, sequences, ``date - date`` (fractional days),
    ``ADD_MONTHS``/``MONTHS_BETWEEN``, dates compared with text (depends on NLS settings),
    numbers or dates joined into text (formatting differs).
Teradata
  * ``ZEROIFNULL``/``NULLIFZERO``; ``date + n`` adds n days; ``date - date`` counts days;
    ``NOT CASESPECIFIC`` columns are compared upper-cased (Teradata ignores case for them).
  * Flagged: grouping or distinct on ``NOT CASESPECIFIC`` columns (Teradata merges 'abc' and
    'ABC'), ``SAMPLE`` (random rows), ``CAST ... FORMAT`` with formats not proven.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from sqlglot import exp, transforms

from accelerator.ir.models import ColumnIR, TableIR
from accelerator.plugins.generators.types import read_oracle, read_teradata

Finding = tuple[str, str]

_INT_TSQL = {"INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "BIT"}
_STRING_FUNCS = (
    exp.Upper,
    exp.Lower,
    exp.Trim,
    exp.Substring,
    exp.Concat,
    exp.DPipe,
    exp.Initcap,
    exp.Pad,
    exp.TimeToStr,
)
_KNOWN_ANON = {"LEN"}
_ORACLE_FMT = {"YYYY": "%Y", "MM": "%m", "DD": "%d", "HH24": "%H", "MI": "%M", "SS": "%S"}
_TRUNC_UNITS = {
    "DD": "DAY",
    "DDD": "DAY",
    "J": "DAY",
    "MM": "MONTH",
    "MON": "MONTH",
    "MONTH": "MONTH",
    "YYYY": "YEAR",
    "YEAR": "YEAR",
    "YY": "YEAR",
    "HH": "HOUR",
    "HH24": "HOUR",
    "MI": "MINUTE",
    "Q": "QUARTER",
}


class ColumnTyper:
    """Find the catalog column behind a column reference in one query."""

    def __init__(self, tree: exp.Expression, find_table: Callable[[str], TableIR | None]) -> None:
        self.by_alias: dict[str, TableIR] = {}
        self.tables: list[TableIR] = []
        for t in tree.find_all(exp.Table):
            raw = ".".join(p for p in (t.catalog, t.db, t.name) if p)
            found = find_table(raw) if raw else None
            if found is None:
                continue
            self.tables.append(found)
            self.by_alias[(t.alias_or_name or t.name).lower()] = found
            self.by_alias.setdefault(t.name.lower(), found)

    def column(self, c: exp.Column) -> ColumnIR | None:
        name = c.name.lower()
        if c.table:
            t = self.by_alias.get(c.table.lower())
            return next((x for x in t.columns if x.name.lower() == name), None) if t else None
        hits = [x for t in self.tables for x in t.columns if x.name.lower() == name]
        return hits[0] if len(hits) == 1 else None


class _Rules:
    def __init__(self, source_db: str, typer: ColumnTyper) -> None:
        self.db = source_db
        self.typer = typer
        self.findings: list[Finding] = []

    def flag(self, code: str, detail: str) -> None:
        if (code, detail) not in self.findings:
            self.findings.append((code, detail))

    # -- typing -------------------------------------------------------------------------------
    def kind(self, e: exp.Expression) -> str:
        """int | number | string | date | timestamp | unknown"""
        e = e.unnest()
        if isinstance(e, exp.Literal):
            if e.is_string:
                return "string"
            return "int" if re.fullmatch(r"\d+", e.name) else "number"
        if isinstance(e, exp.Neg):
            return self.kind(e.this)
        if isinstance(e, (exp.Add, exp.Sub, exp.Mul)):
            a, b = self.kind(e.this), self.kind(e.expression)
            if a in ("date", "timestamp") or b in ("date", "timestamp"):
                return "unknown"
            if a == b == "int":
                return "int"
            return "number" if {a, b} <= {"int", "number"} else "unknown"
        if isinstance(e, (exp.CurrentTimestamp,)):
            return "timestamp"
        if isinstance(e, exp.CurrentDate):
            return "timestamp" if self.db == "oracle" else "date"
        if isinstance(e, (exp.TimestampTrunc, exp.DateTrunc, exp.StrToTime, exp.DateAdd)):
            return "timestamp"
        if isinstance(e, exp.StrToDate):
            return "timestamp" if self.db == "oracle" else "date"
        if isinstance(e, _STRING_FUNCS):
            return "string"
        if isinstance(e, exp.Column):
            col = self.typer.column(e)
            return self.col_kind(col) if col else "unknown"
        if isinstance(e, exp.Cast):
            t = e.to.sql().upper()
            if t.startswith(("VARCHAR", "CHAR", "TEXT")):
                return "string"
            if t.startswith(("INT", "BIGINT", "SMALLINT")):
                return "int"
            if t.startswith("DATE"):
                return "date"
        return "unknown"

    def col_kind(self, col: ColumnIR) -> str:
        if self.db == "oracle":
            nk = read_oracle(col.data_type).kind
        elif self.db == "teradata":
            nk = read_teradata(col.data_type).kind
        else:
            base = re.split(r"[(\s]", col.data_type.strip().upper())[0]
            if base in _INT_TSQL:
                return "int"
            if base in ("DECIMAL", "NUMERIC", "MONEY", "FLOAT", "REAL"):
                return "number"
            if base == "DATE":
                return "date"
            if base.startswith("DATETIME") or base == "SMALLDATETIME":
                return "timestamp"
            if base in ("VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "TEXT", "NTEXT"):
                return "string"
            return "unknown"
        if nk.startswith("int"):
            return "int"
        if nk == "decimal":
            return "int" if (col.scale or 0) == 0 and self.db == "teradata" else "number"
        if nk in ("float32", "float64"):
            return "number"
        if nk in ("string", "char", "text"):
            return "string"
        if nk in ("timestamp", "timestamptz"):
            return "timestamp"
        if nk == "date":
            return "date"
        return "unknown"

    def case_insensitive(self, e: exp.Expression) -> bool:
        return any(
            (col := self.typer.column(c)) is not None and col.attributes.get("case_insensitive")
            for c in e.find_all(exp.Column)
        )

    # -- rules --------------------------------------------------------------------------------
    def group_by_position(self, tree: exp.Expression) -> None:
        for sel in tree.find_all(exp.Select):
            grp = sel.args.get("group")
            if not grp:
                continue
            items = []
            for g in grp.expressions:
                if isinstance(g, exp.Literal) and not g.is_string and g.name.isdigit():
                    idx = int(g.name) - 1
                    if 0 <= idx < len(sel.expressions):
                        items.append(sel.expressions[idx].unalias().copy())
                        continue
                items.append(g)
            grp.set("expressions", items)

    def integer_division(self, tree: exp.Expression) -> None:
        def fix(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Div):
                a, b = self.kind(node.this), self.kind(node.expression)
                if a == b == "int":
                    return exp.IntDiv(this=node.this, expression=node.expression)
                if "int" in (a, b) and "unknown" in (a, b):
                    self.flag(
                        "DIVISION_TYPE_UNKNOWN",
                        f"{node.sql()}: integer division drops "
                        "the remainder on the legacy database; operand type unknown.",
                    )
            return node

        tree.transform(fix, copy=False)

    def date_arithmetic(self, node: exp.Expression) -> exp.Expression:
        if not isinstance(node, (exp.Add, exp.Sub)):
            return node
        a, b = node.this, node.expression
        ka, kb = self.kind(a), self.kind(b)
        dates = ("date", "timestamp")
        if ka in dates and kb in dates and isinstance(node, exp.Sub):
            if self.db == "teradata" and ka == kb == "date":
                return exp.DateDiff(this=a, expression=b, unit=exp.var("DAY"))
            self.flag(
                "DATE_DIFFERENCE",
                f"{node.sql()}: the legacy result is a number of days "
                "(possibly fractional); confirm how it should be computed.",
            )
            return node
        if (ka in dates and kb in ("int", "number")) or (
            kb in dates and ka in ("int", "number") and isinstance(node, exp.Add)
        ):
            d, n = (a, b) if ka in dates else (b, a)
            if self.kind(n) != "int":
                self.flag("DATE_FRACTIONAL_DAYS", f"{node.sql()}: adds a fraction of a day.")
                return node
            amount = exp.Neg(this=exp.Paren(this=n)) if isinstance(node, exp.Sub) else n
            return exp.DateAdd(this=d, expression=amount, unit=exp.var("DAY"))
        return node

    def oracle(self, tree: exp.Expression) -> exp.Expression:
        if "(+)" in tree.sql(dialect="oracle"):
            try:
                tree = transforms.eliminate_join_marks(tree)
            except Exception:
                self.flag("ORACLE_OUTER_JOIN_OPERATOR", "(+) outer join could not be rewritten.")
        if tree.args.get("connect") or any(
            s.args.get("connect") for s in tree.find_all(exp.Select)
        ):
            self.flag("ORACLE_CONNECT_BY", "Hierarchical query (CONNECT BY).")
        for c in tree.find_all(exp.Column):
            n = c.name.upper()
            if n in ("ROWNUM", "ROWID", "LEVEL") and not c.table:
                self.flag(f"ORACLE_{n}", f"Uses the pseudo-column {n}.")
            if n in ("NEXTVAL", "CURRVAL") and c.table:
                self.flag("ORACLE_SEQUENCE", f"Uses the sequence {c.table}.{n}.")
        for f in tree.find_all(exp.Anonymous):
            if f.name.upper() in ("ADD_MONTHS", "MONTHS_BETWEEN", "NEXT_DAY"):
                self.flag("ORACLE_MONTH_ARITHMETIC", f"{f.name}: month-end rules differ by target.")
        if hasattr(exp, "AddMonths") and any(True for _ in tree.find_all(exp.AddMonths)):
            self.flag("ORACLE_MONTH_ARITHMETIC", "ADD_MONTHS: month-end rules differ by target.")

        def fix(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Literal) and node.is_string and node.name == "":
                return exp.Null()  # Oracle stores '' as NULL
            if isinstance(node, exp.DecodeCase):
                args = node.expressions
                subject, rest = args[0], args[1:]
                default = rest.pop() if len(rest) % 2 == 1 else None
                case = exp.Case()
                for s, r in zip(rest[0::2], rest[1::2], strict=True):
                    if isinstance(s, exp.Null):
                        test: exp.Expression = exp.Is(this=subject.copy(), expression=exp.Null())
                    elif isinstance(s, exp.Literal):
                        test = exp.EQ(this=subject.copy(), expression=s)
                    else:
                        test = exp.or_(
                            exp.EQ(this=subject.copy(), expression=s.copy()),
                            exp.and_(
                                exp.Is(this=subject.copy(), expression=exp.Null()),
                                exp.Is(this=s.copy(), expression=exp.Null()),
                            ),
                        )
                    case = case.when(test, r)
                return case.else_(default) if default is not None else case
            if isinstance(node, exp.Substring):
                start = node.args.get("start")
                if isinstance(start, exp.Literal) and start.name == "0":
                    node.set("start", exp.Literal.number(1))
                elif isinstance(start, exp.Neg) and isinstance(start.this, exp.Literal):
                    node.set(
                        "start",
                        exp.Add(
                            this=exp.Length(this=node.this.copy()),
                            expression=exp.Literal.number(1 - int(start.this.name)),
                        ),
                    )
                elif start is not None and not isinstance(start, exp.Literal):
                    self.flag(
                        "ORACLE_SUBSTR_START",
                        f"{node.sql()}: start position 0 or negative "
                        "behaves differently; confirm the values.",
                    )
            if isinstance(node, exp.Anonymous) and node.name.upper() == "TRUNC":
                arg = node.expressions[0] if node.expressions else None
                if arg is not None and self.kind(arg) in ("date", "timestamp") and len(
                        node.expressions) == 1:  # fmt: skip
                    return exp.TimestampTrunc(this=arg, unit=exp.var("DAY"))
                if arg is not None and self.kind(arg) in ("int", "number"):
                    return exp.Trunc(this=arg)
            if isinstance(node, exp.DateTrunc):
                unit = node.text("unit").upper().strip("'")
                if unit not in _TRUNC_UNITS.values():
                    mapped = _TRUNC_UNITS.get(unit)
                    if mapped is None:
                        self.flag("ORACLE_TRUNC_FORMAT", f"TRUNC format {unit}.")
                    else:
                        return exp.TimestampTrunc(this=node.this, unit=exp.var(mapped))
                return exp.TimestampTrunc(this=node.this, unit=exp.var(unit))
            if isinstance(node, exp.ToChar):
                arg, fmt = node.this, node.args.get("format")
                k = self.kind(arg)
                if fmt is None and k == "int":
                    return exp.cast(arg, "VARCHAR")
                if k in ("date", "timestamp") and isinstance(fmt, exp.Literal):
                    tokens = re.findall(r"YYYY|MM|DD|HH24|MI|SS|[-/ :.]|.", fmt.name.upper())
                    if all(t in _ORACLE_FMT or re.fullmatch(r"[-/ :.]", t) for t in tokens):
                        py = "".join(_ORACLE_FMT.get(t, t) for t in tokens)
                        # The cast makes it a date format on every target (TO_CHAR is overloaded).
                        return exp.TimeToStr(
                            this=exp.cast(arg, "TIMESTAMP"), format=exp.Literal.string(py)
                        )
                self.flag("ORACLE_TO_CHAR", f"{node.sql()}: the text format differs between tools.")
            if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
                for x, y in ((node.this, node.expression), (node.expression, node.this)):
                    if self.kind(x) in ("date", "timestamp") and isinstance(y, exp.Literal) and \
                            y.is_string:  # fmt: skip
                        self.flag(
                            "ORACLE_NLS_DATE_LITERAL",
                            f"{node.sql()}: compares a date with "
                            "text; the meaning depends on NLS_DATE_FORMAT.",
                        )
            return self.date_arithmetic(node)

        tree = tree.transform(fix)

        def concat(node: exp.Expression) -> exp.Expression:
            if not isinstance(node, exp.DPipe) or isinstance(node.parent, exp.DPipe):
                return node
            parts: list[exp.Expression] = []

            def flat(e: exp.Expression) -> None:
                if isinstance(e, exp.DPipe):
                    flat(e.this)
                    flat(e.expression)
                else:
                    parts.append(e)

            flat(node)
            texts, nullable = [], []
            for p in parts:
                k = self.kind(p)
                if isinstance(p, exp.Literal) and p.is_string:
                    texts.append(p)
                    continue
                if k == "int":
                    p = exp.cast(p, "VARCHAR")
                elif k != "string":
                    self.flag(
                        "ORACLE_IMPLICIT_TO_CHAR",
                        f"{p.sql()} is joined into text; Oracle formats it with its own rules.",
                    )
                nullable.append(p)
                texts.append(exp.Coalesce(this=p.copy(), expressions=[exp.Literal.string("")]))
            joined = texts[0]
            for t in texts[1:]:
                joined = exp.DPipe(this=joined, expression=t)
            if len(nullable) == len(parts):  # every part may be NULL: all NULL gives NULL
                all_null = exp.and_(
                    *(exp.Is(this=p.copy(), expression=exp.Null()) for p in nullable)
                )
                return exp.Case(ifs=[exp.If(this=all_null, true=exp.Null())], default=joined)
            return exp.Paren(this=joined)

        return tree.transform(concat)

    def teradata(self, tree: exp.Expression) -> exp.Expression:
        if any(isinstance(n, exp.TableSample) for n in tree.walk()):
            self.flag("TD_SAMPLE", "SAMPLE returns random rows; results can't be reconciled.")
        for sel in tree.find_all(exp.Select):
            grouped = list((sel.args.get("group") or exp.Group()).expressions)
            if sel.args.get("distinct"):
                grouped += sel.expressions
            for w in sel.find_all(exp.Window):
                grouped += list(w.args.get("partition_by") or [])
            for g in grouped:
                if self.case_insensitive(g):
                    self.flag(
                        "TD_CASE_INSENSITIVE_GROUPING",
                        f"{g.sql()}: Teradata groups "
                        "'abc' and 'ABC' together (NOT CASESPECIFIC); the target won't.",
                    )

        def fix(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Anonymous):
                name = node.name.upper()
                if name == "ZEROIFNULL" and len(node.expressions) == 1:
                    return exp.Coalesce(
                        this=node.expressions[0], expressions=[exp.Literal.number(0)]
                    )
                if name == "NULLIFZERO" and len(node.expressions) == 1:
                    return exp.Nullif(this=node.expressions[0], expression=exp.Literal.number(0))
            if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like)) and (
                self.case_insensitive(node.this) or self.case_insensitive(node.expression)
            ):
                node.set("this", exp.Upper(this=node.this))
                node.set("expression", exp.Upper(this=node.expression))
            if isinstance(node, exp.In) and self.case_insensitive(node.this):
                node.set("this", exp.Upper(this=node.this))
                node.set("expressions", [exp.Upper(this=e) for e in node.expressions])
            return self.date_arithmetic(node)

        return tree.transform(fix)

    def unknown_functions(self, tree: exp.Expression) -> None:
        for f in tree.find_all(exp.Anonymous):
            if f.name.upper() not in _KNOWN_ANON:
                self.flag(
                    "UNKNOWN_FUNCTION",
                    f"{f.name}() is not known to the translator; its "
                    "behaviour on the target is unverified.",
                )


def apply_source_semantics(
    tree: exp.Expression, source_db: str, find_table: Callable[[str], TableIR | None]
) -> tuple[exp.Expression, list[Finding]]:
    """Return the rewritten tree and anything a person must review."""
    tree = tree.copy()
    rules = _Rules(source_db, ColumnTyper(tree, find_table))
    rules.group_by_position(tree)
    if source_db == "oracle":
        tree = rules.oracle(tree)
    elif source_db == "teradata":
        tree = rules.teradata(tree)
    if source_db in ("teradata", "sqlserver"):
        rules.integer_division(tree)
    rules.unknown_functions(tree)
    return tree, rules.findings
