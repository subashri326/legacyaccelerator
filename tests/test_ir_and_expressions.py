"""Tests for Neutral IR, expression normalization, and multi-dialect transpilation."""

from __future__ import annotations

from accelerator.ir.expressions import ExpressionConverter


def test_ssis_expression_to_neutral_and_targets() -> None:
    expr = ExpressionConverter.to_neutral(
        "e1",
        "gross_amount - (ISNULL(discount) ? 0 : discount)",
        "ssis",
        target_column="net_amount",
    )
    assert not expr.is_opaque
    assert "CASE WHEN" in expr.neutral_sql.upper()
    assert "IS NULL" in expr.neutral_sql.upper()

    sf_sql = ExpressionConverter.render_target(expr.neutral_sql, "snowflake")
    db_sql = ExpressionConverter.render_target(expr.neutral_sql, "databricks")
    fb_sql = ExpressionConverter.render_target(expr.neutral_sql, "fabric")
    assert "CASE" in sf_sql.upper()
    assert "CASE" in db_sql.upper()
    assert "CASE" in fb_sql.upper()


def test_informatica_and_datastage_expressions() -> None:
    infa = ExpressionConverter.to_neutral("e2", "IIF(ISNULL(amt), 0, amt)", "informatica")
    assert not infa.is_opaque
    assert "CASE WHEN" in infa.neutral_sql.upper()

    ds = ExpressionConverter.to_neutral("e3", "NullToValue(name, 'UNKNOWN')", "datastage")
    assert not ds.is_opaque
    assert "COALESCE" in ds.neutral_sql.upper()
