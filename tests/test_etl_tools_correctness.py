"""Proof that converted Informatica and DataStage jobs return the same rows as the legacy logic.

Same method as test_conversion_correctness: the Acme demo runs Discover -> Assess -> Convert ->
Move on DuckDB stand-ins for each target platform, every generated model is translated from the
target dialect to DuckDB and run on the migrated data, and the result is compared with a
hand-written query of what the legacy job does, run on the legacy database.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from test_conversion_correctness import _artifacts, _norm, _run_model, _setup

from accelerator.db.models import ResidueItem
from accelerator.db.session import session_scope

# --- Informatica wf_acme_orders, written by hand from the mapping logic ----------------------
_ORDERS_BASE = """
WITH o AS (
  SELECT order_id, customer_id, quantity,
         CASE WHEN discount IS NULL THEN gross_amount ELSE gross_amount - discount END AS net
  FROM dbo.orders WHERE quantity > 1
), c AS (  -- lookup with its source filter; no match gives NULLs (row kept)
  SELECT customer_id, segment, full_name FROM dbo.customers WHERE created_at >= '2026-01-10'
)
SELECT o.order_id, o.customer_id, o.net,
       CASE WHEN o.net >= 300 THEN 'LARGE' WHEN o.net >= 100 THEN 'MEDIUM' ELSE 'SMALL' END AS band,
       'ORD-' || CAST(o.order_id AS VARCHAR) AS label,
       c.segment, c.full_name
FROM o LEFT JOIN c ON c.customer_id = o.customer_id
"""
INFA_EXPECTED = {
    # Router sends a row to EVERY group it matches (a LARGE SMB order lands in both).
    "priority_orders": f"""SELECT order_id, full_name, net, band, label FROM ({_ORDERS_BASE}) x
        WHERE band = 'LARGE' OR segment = 'ENT'""",
    "smb_orders": f"SELECT order_id, net FROM ({_ORDERS_BASE}) x WHERE segment = 'SMB'",
    "other_orders": f"""SELECT order_id, customer_id, segment FROM ({_ORDERS_BASE}) x
        WHERE NOT COALESCE(band = 'LARGE' OR segment = 'ENT', FALSE)
          AND NOT COALESCE(segment = 'SMB', FALSE)""",
    "order_facts": f"SELECT order_id, net, band, segment FROM ({_ORDERS_BASE}) x",
    "customer_totals": f"""SELECT customer_id, SUM(net), COUNT(order_id),
        COUNT(CASE WHEN band = 'LARGE' THEN order_id END) FROM ({_ORDERS_BASE}) x
        GROUP BY customer_id""",
    # Master outer join keeps every order; the session override (< 150) beats the mapping (< 100).
    "order_lines": """SELECT o.order_id, o.product_id,
        CASE WHEN p.product_name IS NULL THEN 'UNKNOWN'
             ELSE UPPER(SUBSTRING(p.product_name, 1, 3)) || '-' || RIGHT(p.product_name, 1) END,
        o.quantity * p.unit_price
        FROM dbo.orders o
        LEFT JOIN (SELECT * FROM dbo.products WHERE unit_price < 150) p
          ON p.product_id = o.product_id""",
    "customer_list": """SELECT customer_id, full_name FROM dbo.customers WHERE segment = 'ENT'
        UNION ALL
        SELECT customer_id, full_name FROM dbo.customers WHERE segment = 'SMB' AND customer_id <= 6""",
}
INFA_COUNTS = {"order_facts": 96, "order_lines": 120, "customer_list": 13}


@pytest.mark.parametrize("platform", ["snowflake", "databricks", "fabric"])
def test_informatica_converted_code_returns_same_rows(isolated_env: Path, platform: str) -> None:
    pid, src, tgt = _setup(isolated_env, platform)
    files = _artifacts(pid)
    sources = files["dbt/models/sources.yml"]
    legacy = duckdb.connect(str(src), read_only=True)
    target = duckdb.connect(str(tgt), read_only=True)
    try:
        for name, sql in INFA_EXPECTED.items():
            expected = _norm(legacy.execute(sql).fetchall())
            model = files[f"dbt/models/wf_acme_orders__{name}.sql"]
            actual = _norm(_run_model(model, sources, platform, target))
            assert actual == expected, f"{platform}: {name} differs"
            if name in INFA_COUNTS:
                assert len(actual) == INFA_COUNTS[name], name
        # The all-match router really does put some rows in two groups.
        both = legacy.execute(
            f"SELECT COUNT(*) FROM ({_ORDERS_BASE}) x WHERE band = 'LARGE' AND segment = 'SMB'"
        ).fetchone()
        assert both and both[0] > 0
    finally:
        legacy.close()
        target.close()


def test_informatica_logic_without_exact_sql_is_flagged(isolated_env: Path) -> None:
    _setup(isolated_env, "snowflake")
    with session_scope() as db:
        items = db.query(ResidueItem).filter(ResidueItem.object_key == "wf_acme_orders").all()
        found = {(i.component_name, i.reason_code) for i in items}
    assert ("UPD_status", "INFA_UPDATE_STRATEGY") in found
    assert ("SEQ_order_key", "INFA_SEQUENCE_GENERATOR") in found
    assert ("cmd_archive_files", "INFA_COMMAND_TASK") in found
    assert ("EXP_status", "EXPRESSION_OPAQUE") in found  # :LKP call and $$LOAD_DATE parameter
    # Nothing in the three proven mappings needed review.
    proven = {"SQ_orders", "EXP_net", "LKP_customer", "RTR_segment", "AGG_customer",
              "JNR_order_product", "EXP_line", "UN_customers"}  # fmt: skip
    assert not {n for n, _ in found} & proven, found


# --- DataStage acme_jobs.dsx, written by hand from the job logic ------------------------------
_DS_BASE = """
WITH e AS (  -- Lookup with "continue": orders without a (recent) customer keep NULLs
  SELECT o.order_id, o.customer_id, o.gross_amount, o.discount, c.full_name, c.segment
  FROM dbo.orders o
  LEFT JOIN (SELECT customer_id, full_name, segment FROM dbo.customers
             WHERE created_at >= '2026-01-05') c ON c.customer_id = o.customer_id
), n AS (
  SELECT *, CASE WHEN discount IS NULL THEN gross_amount ELSE gross_amount - discount END AS net
  FROM e
)
SELECT *, CASE WHEN net >= 300 THEN 'LARGE' WHEN net >= 100 THEN 'MEDIUM' ELSE 'SMALL' END AS band
FROM n
"""
DS_EXPECTED = {
    # Transformer links each get every row whose constraint holds (a row can go to several).
    "large_orders": f"""SELECT order_id, COALESCE(full_name, 'UNKNOWN'), net,
        'ORD-' || CAST(order_id AS VARCHAR), RIGHT(full_name, 2) FROM ({_DS_BASE}) x
        WHERE band = 'LARGE'""",
    "ent_orders": f"SELECT order_id, net, band FROM ({_DS_BASE}) x WHERE segment = 'ENT'",
    # The otherwise link gets the rows no other link took.
    "other_orders": f"""SELECT order_id, segment, band FROM ({_DS_BASE}) x
        WHERE band <> 'LARGE' AND NOT COALESCE(segment = 'ENT', FALSE)""",
    "customer_totals": """SELECT customer_id, SUM(gross_amount - COALESCE(discount, 0)),
        MAX(gross_amount - COALESCE(discount, 0)), COUNT(*) FROM dbo.orders GROUP BY customer_id""",
    "order_lines": """SELECT o.order_id, o.product_id,
        CASE WHEN p.product_name IS NULL THEN 'UNKNOWN'
             ELSE UPPER(SUBSTRING(p.product_name, 1, 3)) || '-' || RIGHT(p.product_name, 1) END,
        o.quantity * p.unit_price
        FROM dbo.orders o
        LEFT JOIN (SELECT * FROM dbo.products WHERE unit_price < 150) p
          ON p.product_id = o.product_id""",
    # Funnel + Sort + Remove Duplicates (keep first per customer).
    "customer_list": """SELECT DISTINCT customer_id, full_name FROM dbo.customers
        WHERE segment = 'ENT' OR customer_id <= 6""",
}
DS_COUNTS = {"order_lines": 120, "customer_list": 13}


@pytest.mark.parametrize("platform", ["snowflake", "databricks", "fabric"])
def test_datastage_converted_code_returns_same_rows(isolated_env: Path, platform: str) -> None:
    pid, src, tgt = _setup(isolated_env, platform)
    files = _artifacts(pid)
    sources = files["dbt/models/sources.yml"]
    legacy = duckdb.connect(str(src), read_only=True)
    target = duckdb.connect(str(tgt), read_only=True)
    try:
        for name, sql in DS_EXPECTED.items():
            expected = _norm(legacy.execute(sql).fetchall())
            model = files[f"dbt/models/acme_jobs__{name}.sql"]
            actual = _norm(_run_model(model, sources, platform, target))
            assert actual == expected, f"{platform}: {name} differs"
            if name in DS_COUNTS:
                assert len(actual) == DS_COUNTS[name], name
        overlap = legacy.execute(
            f"SELECT COUNT(*) FROM ({_DS_BASE}) x WHERE band = 'LARGE' AND segment = 'ENT'"
        ).fetchone()
        assert overlap and overlap[0] > 0  # the all-match behaviour is really exercised
    finally:
        legacy.close()
        target.close()


def test_datastage_logic_without_exact_sql_is_flagged(isolated_env: Path) -> None:
    _setup(isolated_env, "snowflake")
    with session_scope() as db:
        items = db.query(ResidueItem).filter(ResidueItem.object_key == "acme_jobs").all()
        found = {(i.component_name, i.reason_code) for i in items}
    assert ("Mod_Types", "DS_MODIFY_STAGE") in found
    assert ("Tgt_Status", "DS_TARGET_WRITE_MODE") in found  # upsert, not a plain insert
    assert ("Archive_Files", "DS_SEQUENCE_ACTIVITY") in found
    names = {n for n, _ in found}
    assert any(n.startswith("Xfm_Running") for n in names)  # running total + #pRegion#
    proven = {"Lkp_Customer", "Copy_Enriched", "Xfm_Classify", "Xfm_Net", "Agg_Customer",
              "Join_Products", "Xfm_Line", "Funnel_Customers", "Sort_Customers",
              "RemDup_Customers"}  # fmt: skip
    assert not {n.split(".")[0] for n in names} & proven, found


def test_approved_items_stay_approved_and_their_code_is_used(isolated_env: Path) -> None:
    """Found in the live demo: approving unmapped columns, filters and expressions did not stick;
    the next run raised them again."""
    from accelerator.engines.convert import ConvertEngine

    pid, _src, _tgt = _setup(isolated_env, "databricks")
    code_for = {
        "FILTER_CONDITION_UNKNOWN": "region = 'EMEA'",
        "EXPRESSION_OPAQUE": "SUM(gross_amount) OVER (PARTITION BY customer_id ORDER BY order_id)",
    }
    jobs = ("acme_jobs", "wf_acme_orders", "Load_Finance_GL")
    with session_scope() as db:
        items = [
            i
            for i in db.query(ResidueItem).filter(ResidueItem.project_id == pid).all()
            if i.object_key.split(".")[-1] in jobs or i.object_key in jobs
        ]
        unmapped = [i for i in items if i.reason_code == "TARGET_COLUMN_UNMAPPED"]
        assert len({i.component_key for i in unmapped}) == len(unmapped)  # one id per column
        kinds = {i.reason_code for i in items}
        assert {"FILTER_CONDITION_UNKNOWN", "EXPRESSION_OPAQUE"} <= kinds
        for i in items:
            ConvertEngine().decide_residue(
                db, pid, i.id, "approved", code_for.get(i.reason_code, ""), "", "test", "admin"
            )
        db.commit()
        ConvertEngine().run(db, pid)
        db.commit()
        again = [
            (i.object_key, i.component_name, i.reason_code)
            for i in db.query(ResidueItem)
            .filter(ResidueItem.project_id == pid, ResidueItem.status == "open")
            .all()
            if i.object_key.split(".")[-1] in jobs or i.object_key in jobs
        ]
    assert again == []
    code = "\n".join(_artifacts(pid).values())
    assert "SUM(gross_amount) OVER (PARTITION BY customer_id ORDER BY order_id)" in code


def test_approved_filter_and_column_values_are_used() -> None:
    from accelerator.config.client_config import ClientConfig, SourceConfig, TargetConfig
    from accelerator.ir.models import (
        ColumnIR,
        DataFlowGraphIR,
        EdgeIR,
        ExpressionIR,
        NodeIR,
        NodeOperator,
        PackageIR,
        TableIR,
    )
    from accelerator.plugins.generators.databricks import DatabricksGenerator

    cfg = ClientConfig(
        client="t",
        source=SourceConfig(database="sqlserver", etl="datastage"),
        target=TargetConfig(platform="databricks", output="dbt"),
    )
    cols = [ColumnIR(name="id", data_type="INT", nullable=False, ordinal_position=1)]
    tables = {
        "acme.dbo.src": TableIR(database="acme", schema_name="dbo", name="src", columns=cols),
        "acme.dbo.tgt": TableIR(
            database="acme",
            schema_name="dbo",
            name="tgt",
            columns=[
                *cols,
                ColumnIR(
                    name="region", data_type="VARCHAR(10)", nullable=False, ordinal_position=2
                ),
            ],
        ),
    }
    flow = DataFlowGraphIR(
        id="f",
        name="f",
        nodes=[
            NodeIR(id="s", name="s", operator=NodeOperator.SOURCE, table_ref="dbo.src"),
            NodeIR(
                id="flt",
                name="flt",
                operator=NodeOperator.FILTER,
                condition=ExpressionIR(id="c", original_expr="#pRegion# = 'EMEA'", is_opaque=True),
            ),
            NodeIR(id="t", name="t", operator=NodeOperator.TARGET, table_ref="dbo.tgt"),
        ],
        edges=[
            EdgeIR(id="e1", source_node_id="s", target_node_id="flt"),
            EdgeIR(id="e2", source_node_id="flt", target_node_id="t"),
        ],
    )
    pkg = PackageIR(
        id="job", name="job", source_tool="datastage", source_artifact_name="j", data_flows=[flow]
    )
    gen = DatabricksGenerator()
    before = gen.generate_package(pkg, tables, cfg)
    assert {r.reason_code for r in before.residues} == {
        "FILTER_CONDITION_UNKNOWN",
        "TARGET_COLUMN_UNMAPPED",
    }
    keys = {r.reason_code: r.component_key for r in before.residues}
    after = gen.generate_package(
        pkg,
        tables,
        cfg,
        accepted_overrides={
            keys["FILTER_CONDITION_UNKNOWN"]: ":pRegion = 'EMEA'",
            keys["TARGET_COLUMN_UNMAPPED"]: "'EMEA'",
        },
    )
    assert after.residues == []
    sql = "\n".join(f.content for f in after.files)
    assert "WHERE :pRegion = 'EMEA'  -- reviewed" in sql
    assert "'EMEA' AS region" in sql
