"""Neutral expression parser, AST serializer, and multi-dialect renderer using sqlglot."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from accelerator.ir.models import ExpressionIR

DIALECT_MAP: dict[str, str] = {
    "tsql": "tsql",
    "sqlserver": "tsql",
    "fabric": "tsql",
    "oracle": "oracle",
    "plsql": "oracle",
    "teradata": "teradata",
    "bteq": "teradata",
    "snowflake": "snowflake",
    "databricks": "databricks",
    "spark": "spark",
    "duckdb": "duckdb",
    "sqlite": "sqlite",
    "neutral": "postgres",
}


@dataclass
class NeutralExpression:
    neutral_sql: str
    ast_json: dict[str, Any]
    is_opaque: bool = False
    opaque_reason: str | None = None


def _ast_to_dict(node: exp.Expression | None) -> dict[str, Any]:
    if node is None:
        return {}
    return {
        "type": type(node).__name__,
        "sql": node.sql(),
        "args": {
            k: (_ast_to_dict(v) if isinstance(v, exp.Expression) else str(v))
            for k, v in node.args.items()
            if v is not None and k != "comments"
        },
    }


def _replace_ternaries(expr: str) -> str:
    """Recursively replace SSIS ternary `cond ? a : b` (even inside parens) with SQL CASE WHEN."""
    # First recursively transform any parenthesized sub-expressions
    out: list[str] = []
    i = 0
    while i < len(expr):
        if expr[i] == "(":
            depth = 1
            j = i + 1
            while j < len(expr) and depth > 0:
                if expr[j] == "(":
                    depth += 1
                elif expr[j] == ")":
                    depth -= 1
                j += 1
            inner = _replace_ternaries(expr[i + 1 : j - 1])
            out.append(f"({inner})")
            i = j
        else:
            out.append(expr[i])
            i += 1

    rebuilt = "".join(out)
    # Find top-level '?' and ':' at depth 0
    depth = 0
    q_idx = -1
    c_idx = -1
    for idx, ch in enumerate(rebuilt):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            if ch == "?" and q_idx == -1:
                q_idx = idx
            elif ch == ":" and q_idx != -1 and c_idx == -1:
                c_idx = idx
                break
    if q_idx != -1 and c_idx != -1:
        cond = rebuilt[:q_idx].strip()
        t_val = rebuilt[q_idx + 1 : c_idx].strip()
        f_val = rebuilt[c_idx + 1 :].strip()
        return f"CASE WHEN {cond} THEN {t_val} ELSE {f_val} END"
    return rebuilt


def normalize_etl_expression(expr_text: str, source_tool: str) -> str:
    """Translate vendor-specific ETL expression idioms (SSIS/Informatica/DataStage) to SQL."""
    text = expr_text.strip()
    tool = source_tool.lower()

    if tool == "ssis":
        # Strip @[User::Var] -> :var
        text = re.sub(r"@\[(?:User|System)::([A-Za-z0-9_]+)\]", r":\1", text)
        # Replace SSIS cast (DT_WSTR, 50)col or (DT_I4)col -> CAST(col AS ...)
        text = re.sub(
            r"\(DT_WSTR\s*,\s*\d+\)\s*([A-Za-z0-9_\.\[\]]+)",
            r"CAST(\1 AS VARCHAR)",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\(DT_STR\s*,\s*\d+\s*,\s*\d+\)\s*([A-Za-z0-9_\.\[\]]+)",
            r"CAST(\1 AS VARCHAR)",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\(DT_I4\)\s*([A-Za-z0-9_\.\[\]]+)", r"CAST(\1 AS INTEGER)", text, flags=re.IGNORECASE
        )
        text = re.sub(
            r"\(DT_NUMERIC\s*,\s*(\d+)\s*,\s*(\d+)\)\s*([A-Za-z0-9_\.\[\]]+)",
            r"CAST(\3 AS DECIMAL(\1,\2))",
            text,
            flags=re.IGNORECASE,
        )
        # ISNULL(x) in SSIS is unary boolean! Convert ISNULL(x) -> (x IS NULL)
        text = re.sub(r"\bISNULL\s*\(\s*([^,\)]+)\s*\)", r"(\1 IS NULL)", text, flags=re.IGNORECASE)
        text = (
            text.replace("==", "=").replace("!=", "<>").replace("&&", " AND ").replace("||", " OR ")
        )
        text = re.sub(r"\bGETDATE\s*\(\s*\)", "CURRENT_TIMESTAMP", text, flags=re.IGNORECASE)
        text = re.sub(r"\bLEN\s*\(", "LENGTH(", text, flags=re.IGNORECASE)
        text = _replace_ternaries(text)
        return text

    if tool == "informatica":
        # IIF(cond, a, b) -> CASE WHEN cond THEN a ELSE b END
        iif_match = re.match(
            r"^IIF\s*\(\s*(.+?)\s*,\s*(.+?)\s*,\s*(.+)\s*\)$", text, flags=re.IGNORECASE
        )
        if iif_match:
            cond, t_val, f_val = iif_match.groups()
            text = f"CASE WHEN {cond} THEN {t_val} ELSE {f_val} END"
        text = re.sub(r"\bISNULL\s*\(\s*([^,\)]+)\s*\)", r"(\1 IS NULL)", text, flags=re.IGNORECASE)
        text = re.sub(r"\bNVL\s*\(", "COALESCE(", text, flags=re.IGNORECASE)
        text = re.sub(r"\bSYSDATE\b", "CURRENT_TIMESTAMP", text, flags=re.IGNORECASE)
        text = re.sub(r"\bSUBSTR\s*\(", "SUBSTRING(", text, flags=re.IGNORECASE)
        return text

    if tool == "datastage":
        ds_if = re.match(r"^If\s+(.+?)\s+Then\s+(.+?)\s+Else\s+(.+)$", text, flags=re.IGNORECASE)
        if ds_if:
            cond, t_val, f_val = ds_if.groups()
            text = f"CASE WHEN {cond} THEN {t_val} ELSE {f_val} END"
        text = re.sub(r"\bNullToValue\s*\(", "COALESCE(", text, flags=re.IGNORECASE)
        text = re.sub(r"\bTrim\s*\(", "TRIM(", text, flags=re.IGNORECASE)
        return text

    return text


class ExpressionConverter:
    """Convert expressions/SQL between legacy dialects, Neutral IR, and target cloud dialects."""

    @staticmethod
    def to_neutral(
        expr_id: str,
        raw_expr: str,
        source_dialect: str,
        target_column: str | None = None,
    ) -> ExpressionIR:
        cleaned = normalize_etl_expression(raw_expr, source_dialect)
        dialect_key = DIALECT_MAP.get(source_dialect.lower(), "tsql")
        try:
            # Parse either as standalone expression or inside SELECT
            parsed = sqlglot.parse_one(cleaned, read=dialect_key)
            neutral_sql = parsed.sql(dialect="postgres")
            return ExpressionIR(
                id=expr_id,
                target_column=target_column,
                original_expr=raw_expr,
                source_dialect=source_dialect,
                neutral_sql=neutral_sql,
                ast_json=_ast_to_dict(parsed),
                is_opaque=False,
            )
        except (SqlglotError, ValueError) as exc:
            return ExpressionIR(
                id=expr_id,
                target_column=target_column,
                original_expr=raw_expr,
                source_dialect=source_dialect,
                neutral_sql="",
                ast_json={},
                is_opaque=True,
                opaque_reason=f"EXPRESSION_PARSE_ERROR: {exc}",
            )

    @staticmethod
    def render_target(neutral_sql: str, target_platform: str) -> str:
        if not neutral_sql.strip():
            return ""
        target_dialect = DIALECT_MAP.get(target_platform.lower(), target_platform.lower())
        parsed = sqlglot.parse_one(neutral_sql, read="postgres")
        return parsed.sql(dialect=target_dialect)

    @staticmethod
    def transpile_sql(
        sql_text: str, source_dialect: str, target_platform: str
    ) -> tuple[str, list[str]]:
        """Transpile full SQL statement(s), returning (transpiled_sql, warnings)."""
        src = DIALECT_MAP.get(source_dialect.lower(), "tsql")
        tgt = DIALECT_MAP.get(target_platform.lower(), "snowflake")
        warnings: list[str] = []
        statements: list[str] = []
        for stmt in sqlglot.parse(sql_text, read=src):
            if stmt is None:
                continue
            statements.append(stmt.sql(dialect=tgt, pretty=True))
        return ";\n".join(statements) + (";" if statements else ""), warnings
