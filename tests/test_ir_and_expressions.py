"""Tests for Neutral IR, expression normalization, and multi-dialect transpilation."""

from __future__ import annotations

import pytest

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


def test_etl_expressions_are_exact_or_flagged() -> None:
    from accelerator.ir.etl_expressions import DEC, INT, STRING, Scope, Untranslatable, translate

    sc = Scope.of({"AMT": DEC, "NAME": STRING, "ID": INT})
    # Informatica: || ignores NULLs; DECODE(TRUE, ...) is a searched CASE; SUBSTR start 0 = 1
    sql, _ = translate("NAME || NAME", "informatica", sc)  # NULL only when both are NULL
    assert "COALESCE" in sql and "IS NULL AND" in sql
    sql, _ = translate("NAME || '-'", "informatica", sc)  # a fixed text part is never NULL
    assert "IS NULL" not in sql
    assert translate("DECODE(TRUE, AMT > 1, 'A', 'B')", "informatica", sc)[0].startswith(
        "CASE WHEN (AMT > 1)"
    )
    assert "SUBSTRING(NAME FROM 1 FOR 2)" in translate("SUBSTR(NAME, 0, 2)", "informatica", sc)[0]
    # Things with no exact equivalent are refused, never approximated
    for bad in ("AMT || 'x'", "IIF(AMT > 1, 2)", ":LKP.L(ID)", "$$P", "DATE_DIFF(A, B, 'DD')"):
        with pytest.raises(Untranslatable):
            translate(bad, "informatica", sc)
    ds = Scope.of({"NAME": STRING}, strip_prefixes={"lnk"})
    assert translate("lnk.NAME[2]", "datastage", ds)[0] == "RIGHT(NAME, 2)"
    with pytest.raises(Untranslatable):
        translate("lnk.NAME : #pRegion#", "datastage", ds)
    with pytest.raises(Untranslatable):
        translate("other.NAME", "datastage", ds)


def test_render_refuses_what_a_target_cannot_do_exactly() -> None:
    from accelerator.ir.expressions import ExpressionNotPortable

    assert ExpressionConverter.render_target("LENGTH(x)", "fabric") == "(LEN(x + 'x') - 1)"
    assert (
        "DATEADD"
        in ExpressionConverter.render_target("(d + (3) * INTERVAL '1 DAY')", "snowflake").upper()
    )
    with pytest.raises(ExpressionNotPortable):
        ExpressionConverter.render_target("LPAD(x, 5, '0')", "fabric")
    with pytest.raises(ExpressionNotPortable):
        ExpressionConverter.render_target("TRUNC(a, 2)", "databricks")
