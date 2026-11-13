"""Translate Informatica and DataStage expression languages into neutral SQL, exactly or not at all.

Each tool has its own expression language with its own rules for NULLs, booleans and type
conversion. This module parses the expression properly (a small tokenizer + precedence parser)
and maps every operator and function to SQL that behaves the same way. Anything without a known
exact equivalent raises ``Untranslatable`` so the caller can raise a review item - it is never
approximated.

Semantics that differ from plain SQL and are handled here:

* Informatica ``||`` and ``CONCAT`` ignore NULL operands (NULL only when every operand is NULL).
* Informatica has no boolean type: TRUE is 1, and a number used as a condition means ``<> 0``.
* Informatica ``SUBSTR`` treats a start of 0 as 1 and a negative start as counting from the end.
* Informatica ``DECODE`` without a default returns NULL; ``DECODE(TRUE, cond, value, ...)`` is a
  searched CASE.
* DataStage ``Trim(s)`` also collapses repeated inner spaces; ``s[n]`` is the last n characters.
* DataStage ``:`` propagates NULL (any NULL operand gives NULL).
* Numbers are only concatenated as text when they are whole numbers; decimal-to-text formatting
  differs between tools, so that case is flagged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot.errors import SqlglotError

# Value kinds tracked through an expression.
STRING, INT, DEC, FLOAT, DATE, BOOL, NULL, UNKNOWN = (
    "string",
    "int",
    "dec",
    "float",
    "date",
    "bool",
    "null",
    "unknown",
)
NUMERIC = {INT, DEC, FLOAT}


class Untranslatable(ValueError):
    """The expression has no exact SQL equivalent; a person must review it."""


def informatica_kind(datatype: str | None, scale: str | int | None = None) -> str:
    t = (datatype or "").strip().lower()
    if t in ("string", "nstring", "text", "ntext", "char", "nchar", "varchar", "varchar2"):
        return STRING
    if t in ("integer", "small integer", "smallint", "bigint", "int"):
        return INT
    if t in ("decimal", "number", "numeric"):
        return INT if str(scale or "0") == "0" else DEC
    if t in ("double", "real", "float"):
        return FLOAT
    if t in ("date/time", "date", "timestamp", "datetime"):
        return DATE
    return UNKNOWN


# JDBC type codes used by DataStage column definitions (SqlType).
_DS_SQLTYPES = {
    "1": STRING,
    "12": STRING,
    "-1": STRING,
    "-8": STRING,
    "-9": STRING,
    "-10": STRING,
    "4": INT,
    "5": INT,
    "-6": INT,
    "-5": INT,
    "-7": INT,
    "2": DEC,
    "3": DEC,
    "6": FLOAT,
    "7": FLOAT,
    "8": FLOAT,
    "91": DATE,
    "93": DATE,
}


def datastage_kind(sql_type: str | None, scale: str | int | None = None) -> str:
    kind = _DS_SQLTYPES.get(str(sql_type or "").strip(), UNKNOWN)
    if kind == DEC and str(scale or "0") == "0":
        return INT
    return kind


@dataclass
class Scope:
    """What names an expression may refer to.

    ``columns`` maps a name (case-insensitive) to (output name, kind). ``variables`` maps a
    variable name to its already-translated (sql, kind), or to an Untranslatable explaining why it
    can't be used. ``pending_variables`` are variables not yet evaluated at this point (a
    reference to one means "the previous row's value", which SQL can't express row by row).
    ``strip_prefixes`` are link names to drop from ``link.column`` references (DataStage).
    With ``loose=True`` unknown names are accepted as columns of unknown type.
    """

    columns: dict[str, tuple[str, str]] = field(default_factory=dict)
    variables: dict[str, tuple[str, str] | Untranslatable] = field(default_factory=dict)
    pending_variables: set[str] = field(default_factory=set)
    strip_prefixes: set[str] = field(default_factory=set)
    loose: bool = False

    @classmethod
    def of(cls, columns: dict[str, str], **kw: Any) -> Scope:
        return cls(columns={k.lower(): (k, v) for k, v in columns.items()}, **kw)


# --------------------------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------------------------
_OPS = [
    "||",
    "<=",
    ">=",
    "<>",
    "!=",
    "^=",
    "=",
    "<",
    ">",
    "+",
    "-",
    "*",
    "/",
    "%",
    "(",
    ")",
    ",",
    ":",
    "[",
    "]",
    "#",
]


@dataclass
class Tok:
    kind: str  # num | str | id | op | eof
    value: str


def _tokenize(text: str, tool: str) -> list[Tok]:
    toks: list[Tok] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if text.startswith("--", i) or text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        quotes = ("'",) if tool == "informatica" else ("'", '"', "\\")
        if ch in quotes:
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise Untranslatable("Unterminated string literal")
                if text[j] == ch:
                    if ch == "'" and j + 1 < n and text[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(text[j])
                j += 1
            toks.append(Tok("str", "".join(buf)))
            i = j + 1
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            m = re.match(r"\d*\.?\d+(?:[eE][+-]?\d+)?", text[i:])
            assert m
            toks.append(Tok("num", m.group(0)))
            i += len(m.group(0))
            continue
        if (
            tool == "informatica"
            and ch == ":"
            and re.match(r":(LKP|SP|MCR|TD)\.", text[i:], re.IGNORECASE)
        ):
            raise Untranslatable(
                "Calls an unconnected lookup, stored procedure or macro (:LKP/:SP/:MCR)"
            )
        if ch.isalpha() or ch in "_$@":
            pat = (
                r"[A-Za-z0-9_$#@]+(?:\.[A-Za-z0-9_$#@]+)*"
                if tool == "datastage"
                else (r"[A-Za-z0-9_$#@]+")
            )
            m = re.match(pat, text[i:])
            assert m
            toks.append(Tok("id", m.group(0)))
            i += len(m.group(0))
            continue
        for op in _OPS:
            if text.startswith(op, i):
                toks.append(Tok("op", op))
                i += len(op)
                break
        else:
            raise Untranslatable(f"Unexpected character {ch!r}")
    toks.append(Tok("eof", ""))
    return toks


# --------------------------------------------------------------------------------------------
# Parser -> small AST of tuples
# --------------------------------------------------------------------------------------------
_CMP = {"=", "<>", "!=", "^=", "<", ">", "<=", ">=", "#"}


class _Parser:
    def __init__(self, toks: list[Tok], tool: str) -> None:
        self.toks = toks
        self.i = 0
        self.tool = tool
        self.concat = "||" if tool == "informatica" else ":"

    def peek(self) -> Tok:
        return self.toks[self.i]

    def take(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def is_kw(self, word: str) -> bool:
        t = self.peek()
        return t.kind == "id" and t.value.upper() == word

    def expect(self, value: str) -> None:
        t = self.take()
        if t.value != value and t.value.upper() != value:
            raise Untranslatable(f"Expected {value!r}, found {t.value!r}")

    def parse(self) -> tuple:
        node = self.expr(0)
        if self.peek().kind != "eof":
            raise Untranslatable(f"Unexpected {self.peek().value!r}")
        return node

    # binding powers: OR 1, AND 2, NOT 3, compare 4, concat 5, + - 6, * / % 7, unary 8
    def infix_bp(self) -> tuple[int, str] | None:
        t = self.peek()
        if t.kind == "id" and t.value.upper() in ("OR", "AND"):
            return (1 if t.value.upper() == "OR" else 2), t.value.upper()
        if t.kind != "op":
            return None
        if t.value in _CMP:
            return 4, t.value
        if t.value == self.concat:
            return 5, "concat"
        if t.value in ("+", "-"):
            return 6, t.value
        if t.value in ("*", "/", "%"):
            return 7, t.value
        return None

    def expr(self, min_bp: int) -> tuple:
        left = self.prefix()
        while True:
            if self.tool == "datastage" and self.peek().value == "[":
                left = self.substring(left)
                continue
            info = self.infix_bp()
            if info is None or info[0] <= min_bp:
                break
            bp, op = info
            self.take()
            right = self.expr(bp)
            left = ("bin", op, left, right)
        return left

    def substring(self, target: tuple) -> tuple:
        self.expect("[")
        first = self.expr(0)
        if self.peek().value == ",":
            self.take()
            second = self.expr(0)
            self.expect("]")
            return ("call", "__SUBSTR_DS", [target, first, second])
        self.expect("]")
        return ("call", "RIGHT", [target, first])

    def prefix(self) -> tuple:
        t = self.take()
        if t.kind == "num":
            return ("num", t.value)
        if t.kind == "str":
            return ("str", t.value)
        if t.kind == "op" and t.value == "(":
            inner = self.expr(0)
            self.expect(")")
            return inner
        if t.kind == "op" and t.value == "*" and self.peek().value == ")":
            return ("star",)
        if t.kind == "op" and t.value == "-":
            return ("neg", self.expr(7))
        if t.kind == "op" and t.value == "+":
            return self.expr(7)
        if t.kind == "id":
            word = t.value.upper()
            if word == "NOT":
                return ("not", self.expr(3))
            if word == "NULL" and self.tool == "informatica":
                return ("null",)
            if word in ("TRUE", "FALSE") and self.tool == "informatica":
                return ("bool", word == "TRUE")
            if word == "IF" and self.tool == "datastage":
                cond = self.expr(0)
                self.expect("THEN")
                a = self.expr(0)
                self.expect("ELSE")
                b = self.expr(0)
                return ("if", cond, a, b)
            if self.peek().value == "(":
                self.take()
                args: list[tuple] = []
                if self.peek().value != ")":
                    args.append(self.expr(0))
                    while self.peek().value == ",":
                        self.take()
                        args.append(self.expr(0))
                self.expect(")")
                return ("call", word, args)
            return ("col", t.value)
        raise Untranslatable(f"Unexpected {t.value!r}")


# --------------------------------------------------------------------------------------------
# Emitter -> neutral SQL (PostgreSQL dialect, rendered per target later)
# --------------------------------------------------------------------------------------------
_DATE_UNITS = {
    "Y": "YEAR",
    "YY": "YEAR",
    "YYY": "YEAR",
    "YYYY": "YEAR",
    "MM": "MONTH",
    "MON": "MONTH",
    "MONTH": "MONTH",
    "D": "DAY",
    "DD": "DAY",
    "DDD": "DAY",
    "DY": "DAY",
    "DAY": "DAY",
    "HH": "HOUR",
    "HH12": "HOUR",
    "HH24": "HOUR",
    "MI": "MINUTE",
    "SS": "SECOND",
}
_FORMAT_TOKEN = re.compile(r"YYYY|YY|MM|DD|HH24|MI|SS|[-/ :.,T]")
_DS_FORMAT = {
    "%yyyy": "YYYY",
    "%yy": "YY",
    "%mm": "MM",
    "%dd": "DD",
    "%hh": "HH24",
    "%nn": "MI",
    "%ss": "SS",
}


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _check_format(fmt: str) -> str:
    """Only date formats whose meaning is identical on every target are accepted."""
    rest = _FORMAT_TOKEN.sub("", fmt.upper())
    if rest or not fmt:
        raise Untranslatable(f"Date format {fmt!r} uses parts whose output differs between tools")
    return fmt.upper()


def _ds_format(fmt: str) -> str:
    out = fmt
    for k, v in _DS_FORMAT.items():
        out = out.replace(k, v)
    return _check_format(out)


class _Emitter:
    def __init__(self, tool: str, scope: Scope, allow_aggregates: bool) -> None:
        self.tool = tool
        self.scope = scope
        self.allow_aggregates = allow_aggregates
        self.in_aggregate = False

    # -- contexts -----------------------------------------------------------------------------
    def value(self, node: tuple) -> tuple[str, str]:
        sql, kind = self.emit(node)
        if kind == BOOL:
            return f"CASE WHEN {sql} THEN 1 ELSE 0 END", INT
        return sql, kind

    def cond(self, node: tuple) -> str:
        sql, kind = self.emit(node)
        if kind == BOOL:
            return sql
        if kind in NUMERIC:
            return f"({sql} <> 0)"
        if kind == NULL:
            return "(1 = 0)"
        raise Untranslatable(f"A {kind} value is used as a condition")

    # -- nodes --------------------------------------------------------------------------------
    def emit(self, node: tuple) -> tuple[str, str]:
        tag = node[0]
        if tag == "num":
            return node[1], (INT if re.fullmatch(r"\d+", node[1]) else DEC)
        if tag == "str":
            return _lit(node[1]), STRING
        if tag == "null":
            return "NULL", NULL
        if tag == "bool":
            return ("(1 = 1)" if node[1] else "(1 = 0)"), BOOL
        if tag == "col":
            return self.column(node[1])
        if tag == "neg":
            sql, kind = self.value(node[1])
            self.need_number(kind, "-")
            return f"(-{sql})", kind
        if tag == "not":
            return f"(NOT {self.cond(node[1])})", BOOL
        if tag == "if":
            a, ka = self.value(node[2])
            b, kb = self.value(node[3])
            return f"CASE WHEN {self.cond(node[1])} THEN {a} ELSE {b} END", self.common(ka, kb)
        if tag == "bin":
            return self.binary(node[1], node[2], node[3])
        if tag == "call":
            return self.call(node[1], node[2])
        raise Untranslatable(f"Unsupported construct {tag}")

    def column(self, raw: str) -> tuple[str, str]:
        name = raw
        if "." in name:
            prefix, _, rest = name.partition(".")
            if prefix.lower() in {p.lower() for p in self.scope.strip_prefixes}:
                name = rest
            else:
                raise Untranslatable(f"Reference to {raw!r} from an unknown link")
        key = name.lower()
        for var, val in self.scope.variables.items():
            if var.lower() == key:
                if isinstance(val, Untranslatable):
                    raise val
                return f"({val[0]})", val[1]
        if key in {v.lower() for v in self.scope.pending_variables}:
            raise Untranslatable(
                f"Variable {name!r} is read before it is set (keeps a value from the previous "
                "row), which needs row-by-row processing"
            )
        if name.startswith(("$", "@")):
            raise Untranslatable(f"Uses the parameter or system variable {name!r}")
        if key in self.scope.columns:
            out, kind = self.scope.columns[key]
            return out, kind
        if self.scope.loose:
            return name, UNKNOWN
        raise Untranslatable(f"Unknown column {name!r}")

    @staticmethod
    def common(a: str, b: str) -> str:
        if a == NULL:
            return b
        if b == NULL or a == b:
            return a
        if a in NUMERIC and b in NUMERIC:
            return FLOAT if FLOAT in (a, b) else DEC
        return UNKNOWN

    @staticmethod
    def need_number(kind: str, what: str) -> None:
        if kind not in NUMERIC | {NULL, UNKNOWN}:
            raise Untranslatable(f"{what} applied to a {kind} value")

    def as_text(self, sql: str, kind: str) -> str:
        if kind in (STRING, NULL):
            return sql
        if kind == INT:
            return f"CAST({sql} AS VARCHAR)"
        if kind == UNKNOWN and self.scope.loose:
            return f"CAST({sql} AS VARCHAR)"
        raise Untranslatable(
            f"Converts a {kind} value to text; the text format differs between tools"
        )

    def flatten_concat(self, node: tuple) -> list[tuple]:
        if node[0] == "bin" and node[1] == "concat":
            return self.flatten_concat(node[2]) + self.flatten_concat(node[3])
        return [node]

    def concat_values(self, parts: list[tuple[str, str]]) -> tuple[str, str]:
        if self.tool == "informatica":  # NULL operands are ignored; all-NULL gives NULL
            texts = [(self.as_text(s, k), k) for s, k in parts]
            joined = " || ".join(
                s if k == STRING and s.startswith("'") else f"COALESCE({s}, '')" for s, k in texts
            )
            nullable = [s for s, k in texts if not (k == STRING and s.startswith("'"))]
            if len(nullable) == len(texts):
                checks = " AND ".join(f"{s} IS NULL" for s in nullable)
                return f"CASE WHEN {checks} THEN NULL ELSE {joined} END", STRING
            return f"({joined})", STRING
        return "(" + " || ".join(self.as_text(s, k) for s, k in parts) + ")", STRING

    def binary(self, op: str, a: tuple, b: tuple) -> tuple[str, str]:
        if op in ("OR", "AND"):
            return f"({self.cond(a)} {op} {self.cond(b)})", BOOL
        if op == "concat":
            parts = [self.value(p) for p in self.flatten_concat(("bin", op, a, b))]
            return self.concat_values(parts)
        sa, ka = self.value(a)
        sb, kb = self.value(b)
        if op in _CMP:
            sql_op = {"!=": "<>", "^=": "<>", "#": "<>"}.get(op, op)
            if {ka, kb} & {STRING} and {ka, kb} & NUMERIC:
                raise Untranslatable("Compares text with a number (implicit conversion)")
            return f"({sa} {sql_op} {sb})", BOOL
        self.need_number(ka, op)
        self.need_number(kb, op)
        if op == "%":
            return f"MOD({sa}, {sb})", self.common(ka, kb)
        if op == "/":
            return f"({sa} / {sb})", FLOAT if FLOAT in (ka, kb) else DEC
        return f"({sa} {op} {sb})", self.common(ka, kb)

    # -- functions ----------------------------------------------------------------------------
    def call(self, name: str, args: list[tuple]) -> tuple[str, str]:
        handler = getattr(self, f"fn_{self.tool}_{name.lower()}", None) or getattr(
            self, f"fn_{name.lower()}", None
        )
        if handler is None or (name.startswith("__") and name != "__SUBSTR_DS"):
            raise Untranslatable(f"Function {name}() has no exact equivalent")
        return handler(args)

    def nargs(self, args: list[tuple], lo: int, hi: int | None = None) -> None:
        hi = lo if hi is None else hi
        if not lo <= len(args) <= hi:
            raise Untranslatable(f"Unexpected number of arguments ({len(args)})")

    def literal_str(self, node: tuple, what: str) -> str:
        if node[0] != "str":
            raise Untranslatable(f"{what} must be a fixed text value")
        return node[1]

    def literal_int(self, node: tuple, what: str) -> int:
        if node[0] == "neg" and node[1][0] == "num" and node[1][1].isdigit():
            return -int(node[1][1])
        if node[0] != "num" or not node[1].isdigit():
            raise Untranslatable(f"{what} must be a fixed whole number")
        return int(node[1])

    def unary(self, args: list[tuple], func: str, kind: str | None = None) -> tuple[str, str]:
        self.nargs(args, 1)
        s, k = self.value(args[0])
        return f"{func}({s})", kind or k

    # shared (both tools)
    def fn_upper(self, a: list[tuple]) -> tuple[str, str]:
        return self.unary(a, "UPPER", STRING)

    def fn_lower(self, a: list[tuple]) -> tuple[str, str]:
        return self.unary(a, "LOWER", STRING)

    def fn_abs(self, a: list[tuple]) -> tuple[str, str]:
        return self.unary(a, "ABS")

    def fn_ceil(self, a: list[tuple]) -> tuple[str, str]:
        return self.unary(a, "CEIL", INT)

    def fn_floor(self, a: list[tuple]) -> tuple[str, str]:
        return self.unary(a, "FLOOR", INT)

    def fn_sqrt(self, a: list[tuple]) -> tuple[str, str]:
        return self.unary(a, "SQRT", FLOAT)

    def fn_mod(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 2)
        (x, kx), (y, ky) = self.value(a[0]), self.value(a[1])
        return f"MOD({x}, {y})", self.common(kx, ky)

    def fn_power(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 2)
        return f"POWER({self.value(a[0])[0]}, {self.value(a[1])[0]})", FLOAT

    # --- Informatica -------------------------------------------------------------------------
    def fn_informatica_iif(self, a: list[tuple]) -> tuple[str, str]:
        if len(a) == 2:
            raise Untranslatable("IIF without an else value returns a type-dependent default")
        self.nargs(a, 3)
        x, kx = self.value(a[1])
        y, ky = self.value(a[2])
        return f"CASE WHEN {self.cond(a[0])} THEN {x} ELSE {y} END", self.common(kx, ky)

    def fn_informatica_decode(self, a: list[tuple]) -> tuple[str, str]:
        if len(a) < 3:
            raise Untranslatable("DECODE needs a value and at least one search/result pair")
        searched = a[0] == ("bool", True)
        subject = None if searched else self.value(a[0])[0]
        pairs, default = a[1:], None
        if len(pairs) % 2 == 1:
            default = pairs[-1]
            pairs = pairs[:-1]
        whens, kind = [], NULL
        for s, r in zip(pairs[0::2], pairs[1::2], strict=True):
            if searched:
                test = self.cond(s)
            elif s == ("null",):
                test = f"{subject} IS NULL"
            else:
                test = f"{subject} = {self.value(s)[0]}"
            rv, rk = self.value(r)
            kind = self.common(kind, rk)
            whens.append(f"WHEN {test} THEN {rv}")
        else_sql = ""
        if default is not None:
            dv, dk = self.value(default)
            kind = self.common(kind, dk)
            else_sql = f" ELSE {dv}"
        return f"CASE {' '.join(whens)}{else_sql} END", kind

    def fn_informatica_isnull(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 1)
        return f"({self.value(a[0])[0]} IS NULL)", BOOL

    def fn_informatica_in(self, a: list[tuple]) -> tuple[str, str]:
        if len(a) < 2:
            raise Untranslatable("IN needs a value and at least one option")
        subject = self.value(a[0])[0]
        opts = [self.value(x)[0] for x in a[1:]]
        return f"({subject} IN ({', '.join(opts)}))", BOOL

    def fn_informatica_length(self, a: list[tuple]) -> tuple[str, str]:
        return self.unary(a, "LENGTH", INT)

    def _trim(self, a: list[tuple], side: str) -> tuple[str, str]:
        self.nargs(a, 1, 2)
        s, _ = self.value(a[0])
        if len(a) == 1:
            return f"{'LTRIM' if side == 'LEADING' else 'RTRIM'}({s})", STRING
        return f"TRIM({side} {_lit(self.literal_str(a[1], 'Trim set'))} FROM {s})", STRING

    def fn_informatica_ltrim(self, a: list[tuple]) -> tuple[str, str]:
        return self._trim(a, "LEADING")

    def fn_informatica_rtrim(self, a: list[tuple]) -> tuple[str, str]:
        return self._trim(a, "TRAILING")

    def fn_informatica_substr(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 2, 3)
        s, _ = self.value(a[0])
        length = f", {self.value(a[2])[0]}" if len(a) == 3 else ""
        if a[1][0] in ("num", "neg"):
            start = self.literal_int(a[1], "SUBSTR start")
            if start >= 0:
                return f"SUBSTRING({s}, {max(start, 1)}{length})", STRING
            return f"SUBSTRING({s}, LENGTH({s}) + {start + 1}{length})", STRING
        st, _ = self.value(a[1])
        return (
            f"CASE WHEN {st} > 0 THEN SUBSTRING({s}, {st}{length}) WHEN {st} = 0 THEN "
            f"SUBSTRING({s}, 1{length}) ELSE SUBSTRING({s}, LENGTH({s}) + {st} + 1{length}) END",
            STRING,
        )

    def fn_informatica_instr(self, a: list[tuple]) -> tuple[str, str]:
        if len(a) != 2:
            raise Untranslatable("INSTR with a start position or occurrence")
        return f"STRPOS({self.value(a[0])[0]}, {self.value(a[1])[0]})", INT

    def fn_informatica_concat(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 2)
        return self.concat_values([self.value(a[0]), self.value(a[1])])

    def fn_informatica_replacestr(self, a: list[tuple]) -> tuple[str, str]:
        # REPLACESTR(case_flag, input, old, new): only the case-sensitive single-search form.
        if len(a) != 4 or a[0] != ("num", "1") or a[2][0] != "str" or a[3][0] != "str":
            raise Untranslatable("REPLACESTR other than the case-sensitive, one-string form")
        s, _ = self.value(a[1])
        return f"REPLACE({s}, {_lit(a[2][1])}, {_lit(a[3][1])})", STRING

    def _pad(self, a: list[tuple], func: str) -> tuple[str, str]:
        self.nargs(a, 2, 3)
        s, _ = self.value(a[0])
        n = self.value(a[1])[0]
        pad = self.value(a[2])[0] if len(a) == 3 else "' '"
        return f"{func}({s}, {n}, {pad})", STRING

    def fn_informatica_lpad(self, a: list[tuple]) -> tuple[str, str]:
        return self._pad(a, "LPAD")

    def fn_informatica_rpad(self, a: list[tuple]) -> tuple[str, str]:
        return self._pad(a, "RPAD")

    def fn_informatica_to_char(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 1, 2)
        s, k = self.value(a[0])
        if k == DATE:
            fmt = (
                self.literal_str(a[1], "TO_CHAR format")
                if len(a) == 2
                else ("MM/DD/YYYY HH24:MI:SS")
            )
            return f"TO_CHAR({s}, {_lit(_check_format(fmt))})", STRING
        if len(a) == 2:
            raise Untranslatable("TO_CHAR of a number with a format")
        return self.as_text(s, k), STRING

    def fn_informatica_to_date(self, a: list[tuple]) -> tuple[str, str]:
        if len(a) != 2:
            raise Untranslatable("TO_DATE without a format depends on session settings")
        s, _ = self.value(a[0])
        fmt = _check_format(self.literal_str(a[1], "TO_DATE format"))
        return f"TO_TIMESTAMP({s}, {_lit(fmt)})", DATE

    def _to_int(self, a: list[tuple], sql_type: str) -> tuple[str, str]:
        self.nargs(a, 1, 2)
        s, k = self.value(a[0])
        if k == STRING:
            s = f"CAST({s} AS DECIMAL(38, 10))"
        truncate = len(a) == 1 or a[1] in (("bool", True), ("num", "1"))
        if not truncate and a[1] not in (("bool", False), ("num", "0")):
            raise Untranslatable("Rounding flag must be TRUE or FALSE")
        return f"CAST({'TRUNC' if truncate else 'ROUND'}({s}) AS {sql_type})", INT

    def fn_informatica_to_integer(self, a: list[tuple]) -> tuple[str, str]:
        return self._to_int(a, "INTEGER")

    def fn_informatica_to_bigint(self, a: list[tuple]) -> tuple[str, str]:
        return self._to_int(a, "BIGINT")

    def fn_informatica_to_decimal(self, a: list[tuple]) -> tuple[str, str]:
        if len(a) != 2:
            raise Untranslatable("TO_DECIMAL without a scale keeps the input's scale")
        scale = self.literal_int(a[1], "TO_DECIMAL scale")
        return f"CAST({self.value(a[0])[0]} AS DECIMAL(28, {scale}))", DEC

    def fn_informatica_to_float(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 1)
        return f"CAST({self.value(a[0])[0]} AS DOUBLE PRECISION)", FLOAT

    def fn_informatica_round(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 1, 2)
        s, k = self.value(a[0])
        if k not in NUMERIC:
            raise Untranslatable(f"ROUND of a {k} value")
        digits = f", {self.literal_int(a[1], 'ROUND precision')}" if len(a) == 2 else ""
        return f"ROUND({s}{digits})", k

    def fn_informatica_trunc(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 1, 2)
        s, k = self.value(a[0])
        if k == DATE:
            unit = _DATE_UNITS.get(
                self.literal_str(a[1], "TRUNC format").upper() if len(a) == 2 else "DD"
            )
            if unit is None:
                raise Untranslatable("TRUNC date format")
            return f"DATE_TRUNC('{unit.lower()}', {s})", DATE
        if k not in NUMERIC:
            raise Untranslatable(f"TRUNC of a {k} value")
        digits = f", {self.literal_int(a[1], 'TRUNC precision')}" if len(a) == 2 else ""
        return f"TRUNC({s}{digits})", k

    def fn_informatica_add_to_date(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 3)
        d, _ = self.value(a[0])
        unit = _DATE_UNITS.get(self.literal_str(a[1], "ADD_TO_DATE format").upper())
        if unit is None:
            raise Untranslatable("ADD_TO_DATE format")
        n, _ = self.value(a[2])
        return f"({d} + ({n}) * INTERVAL '1 {unit}')", DATE

    def fn_informatica_get_date_part(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 2)
        unit = _DATE_UNITS.get(self.literal_str(a[1], "GET_DATE_PART format").upper())
        if unit is None:
            raise Untranslatable("GET_DATE_PART format")
        return f"CAST(EXTRACT({unit} FROM {self.value(a[0])[0]}) AS INTEGER)", INT

    def fn_informatica_sysdate(self, a: list[tuple]) -> tuple[str, str]:
        return "CURRENT_TIMESTAMP", DATE

    def fn_informatica_sessstarttime(self, a: list[tuple]) -> tuple[str, str]:
        return "CURRENT_TIMESTAMP", DATE

    def _agg(self, func: str, a: list[tuple]) -> tuple[str, str]:
        if not self.allow_aggregates or self.in_aggregate:
            raise Untranslatable(f"{func} used outside an Aggregator")
        self.nargs(a, 1, 2)
        self.in_aggregate = True
        try:
            if a[0] == ("star",):
                if func != "COUNT" or len(a) == 2:
                    raise Untranslatable(f"{func}(*)")
                arg, kind = "*", INT
            else:
                arg, kind = self.value(a[0])
            if len(a) == 2:  # Informatica conditional aggregate: SUM(x, condition)
                arg = f"CASE WHEN {self.cond(a[1])} THEN {arg if arg != '*' else 1} END"
        finally:
            self.in_aggregate = False
        out_kind = INT if func == "COUNT" else (DEC if func == "AVG" and kind == INT else kind)
        return f"{func}({arg})", out_kind

    def fn_sum(self, a: list[tuple]) -> tuple[str, str]:
        return self._agg("SUM", a)

    def fn_avg(self, a: list[tuple]) -> tuple[str, str]:
        return self._agg("AVG", a)

    def fn_min(self, a: list[tuple]) -> tuple[str, str]:
        return self._agg("MIN", a)

    def fn_max(self, a: list[tuple]) -> tuple[str, str]:
        return self._agg("MAX", a)

    def fn_count(self, a: list[tuple]) -> tuple[str, str]:
        return self._agg("COUNT", a)

    # --- DataStage ---------------------------------------------------------------------------
    def fn_datastage_trimleadingtrailing(self, a: list[tuple]) -> tuple[str, str]:
        return self.unary(a, "TRIM", STRING)

    def fn_datastage_trimf(self, a: list[tuple]) -> tuple[str, str]:
        return self.unary(a, "LTRIM", STRING)

    def fn_datastage_trimb(self, a: list[tuple]) -> tuple[str, str]:
        return self.unary(a, "RTRIM", STRING)

    def fn_datastage_trim(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 1, 3)
        s, _ = self.value(a[0])
        if len(a) == 1:  # leading, trailing and repeated inner spaces
            return f"TRIM(REGEXP_REPLACE({s}, ' {{2,}}', ' '))", STRING
        ch = self.literal_str(a[1], "Trim character")
        mode = self.literal_str(a[2], "Trim type").upper() if len(a) == 3 else "R"
        if len(ch) != 1:
            raise Untranslatable("Trim character must be one character")
        if mode == "A":
            return f"REPLACE({s}, {_lit(ch)}, '')", STRING
        side = {"L": "LEADING", "T": "TRAILING", "B": "BOTH"}.get(mode)
        if side is None:
            raise Untranslatable(f"Trim type {mode!r}")
        return f"TRIM({side} {_lit(ch)} FROM {s})", STRING

    def fn_datastage_upcase(self, a: list[tuple]) -> tuple[str, str]:
        return self.unary(a, "UPPER", STRING)

    def fn_datastage_downcase(self, a: list[tuple]) -> tuple[str, str]:
        return self.unary(a, "LOWER", STRING)

    def fn_datastage_len(self, a: list[tuple]) -> tuple[str, str]:
        return self.unary(a, "LENGTH", INT)

    def fn_datastage_left(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 2)
        return f"LEFT({self.value(a[0])[0]}, {self.value(a[1])[0]})", STRING

    def fn_right(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 2)
        return f"RIGHT({self.value(a[0])[0]}, {self.value(a[1])[0]})", STRING

    def fn___substr_ds(self, a: list[tuple]) -> tuple[str, str]:
        return f"SUBSTRING({self.value(a[0])[0]}, {self.value(a[1])[0]}, {self.value(a[2])[0]})", (
            STRING
        )

    def fn_datastage_isnull(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 1)
        return f"({self.value(a[0])[0]} IS NULL)", BOOL

    def fn_datastage_isnotnull(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 1)
        return f"({self.value(a[0])[0]} IS NOT NULL)", BOOL

    def fn_datastage_nulltovalue(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 2)
        (x, kx), (y, ky) = self.value(a[0]), self.value(a[1])
        return f"COALESCE({x}, {y})", self.common(kx, ky)

    def fn_datastage_nulltozero(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 1)
        x, k = self.value(a[0])
        return f"COALESCE({x}, 0)", k

    def fn_datastage_nulltoempty(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 1)
        return f"COALESCE({self.value(a[0])[0]}, '')", STRING

    def fn_datastage_setnull(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 0)
        return "NULL", NULL

    def fn_datastage_index(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 3)
        if a[2] != ("num", "1"):
            raise Untranslatable("Index() for an occurrence other than the first")
        return f"STRPOS({self.value(a[0])[0]}, {self.value(a[1])[0]})", INT

    def fn_datastage_field(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 3)
        s = self.value(a[0])[0]
        delim = self.literal_str(a[1], "Field delimiter")
        if len(delim) != 1:
            raise Untranslatable("Field() with a multi-character delimiter")
        return f"SPLIT_PART({s}, {_lit(delim)}, {self.literal_int(a[2], 'Field number')})", STRING

    def fn_datastage_ereplace(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 3)
        return (
            f"REPLACE({self.value(a[0])[0]}, {self.value(a[1])[0]}, {self.value(a[2])[0]})",
            STRING,
        )

    def fn_datastage_currentdate(self, a: list[tuple]) -> tuple[str, str]:
        return "CURRENT_DATE", DATE

    def fn_datastage_currenttimestamp(self, a: list[tuple]) -> tuple[str, str]:
        return "CURRENT_TIMESTAMP", DATE

    def fn_datastage_stringtodate(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 1, 2)
        fmt = _ds_format(self.literal_str(a[1], "date format") if len(a) == 2 else "%yyyy-%mm-%dd")
        return f"TO_DATE({self.value(a[0])[0]}, {_lit(fmt)})", DATE

    def fn_datastage_stringtotimestamp(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 1, 2)
        default = "%yyyy-%mm-%dd %hh:%nn:%ss"
        fmt = _ds_format(self.literal_str(a[1], "timestamp format") if len(a) == 2 else default)
        return f"TO_TIMESTAMP({self.value(a[0])[0]}, {_lit(fmt)})", DATE

    def fn_datastage_datetostring(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 1, 2)
        fmt = _ds_format(self.literal_str(a[1], "date format") if len(a) == 2 else "%yyyy-%mm-%dd")
        return f"TO_CHAR({self.value(a[0])[0]}, {_lit(fmt)})", STRING

    def fn_datastage_timestamptostring(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 1, 2)
        default = "%yyyy-%mm-%dd %hh:%nn:%ss"
        fmt = _ds_format(self.literal_str(a[1], "timestamp format") if len(a) == 2 else default)
        return f"TO_CHAR({self.value(a[0])[0]}, {_lit(fmt)})", STRING

    def _ds_part(self, a: list[tuple], unit: str) -> tuple[str, str]:
        self.nargs(a, 1)
        return f"CAST(EXTRACT({unit} FROM {self.value(a[0])[0]}) AS INTEGER)", INT

    def fn_datastage_yearfromdate(self, a: list[tuple]) -> tuple[str, str]:
        return self._ds_part(a, "YEAR")

    def fn_datastage_monthfromdate(self, a: list[tuple]) -> tuple[str, str]:
        return self._ds_part(a, "MONTH")

    def fn_datastage_monthdayfromdate(self, a: list[tuple]) -> tuple[str, str]:
        return self._ds_part(a, "DAY")

    def fn_datastage_stringtointeger(self, a: list[tuple]) -> tuple[str, str]:
        self.nargs(a, 1)
        return f"CAST({self.value(a[0])[0]} AS INTEGER)", INT


def translate(
    text: str,
    tool: str,
    scope: Scope,
    *,
    context: str = "value",
    allow_aggregates: bool = False,
) -> tuple[str, str]:
    """Return (neutral_sql, kind) for an Informatica or DataStage expression.

    ``context`` is "value" (a column value) or "condition" (a filter/route/constraint).
    Raises Untranslatable when there is no exact equivalent.
    """
    tool = tool.lower()
    if tool not in ("informatica", "datastage"):
        raise ValueError(f"No expression translator for {tool!r}")
    if not text or not text.strip():
        raise Untranslatable("Empty expression")
    param = re.search(r"#([A-Za-z_][\w.]*)#", text) if tool == "datastage" else None
    if param:
        raise Untranslatable(f"Uses the job parameter #{param.group(1)}#")
    tree = _Parser(_tokenize(text, tool), tool).parse()
    em = _Emitter(tool, scope, allow_aggregates)
    if context == "condition":
        sql, kind = em.cond(tree), BOOL
    else:
        sql, kind = em.value(tree)
    try:  # the neutral SQL must itself be valid
        sql = sqlglot.parse_one(sql, read="postgres").sql(dialect="postgres")
    except (SqlglotError, ValueError) as exc:
        raise Untranslatable(f"Generated SQL did not parse: {exc}") from exc
    return sql, kind
