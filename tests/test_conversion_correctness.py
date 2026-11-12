"""Proof that converted code returns the same rows as the legacy logic.

For each target platform the Acme demo runs Discover -> Assess -> Convert -> Move on DuckDB
stand-ins. Each generated model is then translated from the target dialect to DuckDB, run
against the migrated data, and compared with an independent, hand-written query of what the
legacy job does, run against the legacy database.
"""

from __future__ import annotations

import importlib.util
import re
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
import sqlglot
import yaml

from accelerator.config.client_config import ClientConfig, CustomRule
from accelerator.core.secrets import get_vault
from accelerator.core.storage import get_artifact_store
from accelerator.db.models import GeneratedArtifact, ObjectStatus, ResidueItem, StatusEvent
from accelerator.db.repository import ensure_project_from_config
from accelerator.db.session import session_scope
from accelerator.engines.assess import AssessEngine
from accelerator.engines.convert import ConvertEngine
from accelerator.engines.discover import DiscoverEngine
from accelerator.engines.move import MoveEngine
from accelerator.engines.reconcile import ReconcileEngine
from accelerator.ir.expressions import DIALECT_MAP
from accelerator.plugins.registry import build_default_registry

EXAMPLES = Path("examples/acme_retail")

# What the legacy SSIS package Load_Customers_And_Orders produces, written by hand:
# orders over 100, joined to their customer, net amount derived, columns per the destination mapping.
EXPECTED_CURATED_ORDERS = """
SELECT o.order_id, o.customer_id, c.segment AS customer_segment, o.gross_amount,
       o.gross_amount - COALESCE(o.discount, 0) AS net_amount
FROM dbo.orders o JOIN dbo.customers c ON c.customer_id = o.customer_id
WHERE o.gross_amount > 100
"""

# What the legacy view dbo.v_customer_orders returns, written by hand.
EXPECTED_CUSTOMER_ORDERS = """
SELECT c.customer_id, c.full_name, COUNT(o.order_id), SUM(o.gross_amount - COALESCE(o.discount, 0))
FROM dbo.customers c JOIN dbo.orders o ON c.customer_id = o.customer_id
GROUP BY c.customer_id, c.full_name
"""


def _norm(rows: list[tuple]) -> list[tuple]:
    out = []
    for r in rows:
        out.append(tuple(round(float(v), 4) if isinstance(v, (Decimal, float)) else v for v in r))
    return sorted(out, key=repr)


def _run_model(
    content: str, sources_yaml: str, platform: str, conn: duckdb.DuckDBPyConnection
) -> list[tuple]:
    """Execute a generated dbt model on the DuckDB stand-in, resolving source() like dbt would."""
    srcs = yaml.safe_load(sources_yaml)["sources"]
    rel = {
        (s["name"], t["name"]): f"{s['schema']}.{t['identifier']}"
        for s in srcs
        for t in s["tables"]
    }
    body = re.sub(r"\{\{\s*config\([^}]*\)\s*\}\}", "", content)
    body = re.sub(
        r"\{\{\s*source\('(\w+)',\s*'(\w+)'\)\s*\}\}", lambda m: rel[(m.group(1), m.group(2))], body
    )
    sql = sqlglot.transpile(body, read=DIALECT_MAP[platform], write="duckdb")[0]
    return conn.execute(sql).fetchall()


def _setup(tmp: Path, platform: str) -> tuple[str, Path, Path]:
    spec = importlib.util.spec_from_file_location("seed_legacy_db", EXAMPLES / "seed_legacy_db.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    src, tgt = mod.seed_all(base_dir=tmp / f"db_{platform}")
    vault = get_vault()
    vault.set_secret(f"vault://acme/{platform}-src", f"duckdb:///{src.as_posix()}")
    vault.set_secret(f"vault://acme/{platform}-tgt", f"duckdb:///{tgt.as_posix()}")
    raw = (EXAMPLES / "client_config.yaml").read_text(encoding="utf-8")
    raw = raw.replace("platform: snowflake", f"platform: {platform}")
    raw = raw.replace("vault://acme/sqlserver-ro", f"vault://acme/{platform}-src")
    raw = raw.replace("vault://acme/snowflake-deploy", f"vault://acme/{platform}-tgt")
    raw = raw.replace("project: acme_retail_dw", f"project: acme_{platform}")
    with session_scope() as db:
        proj, _, _ = ensure_project_from_config(db, raw)
        pid = proj.id
        DiscoverEngine().run(db, pid)
        AssessEngine().run(db, pid)
        ConvertEngine().run(db, pid)
        MoveEngine().run(db, pid)
    return pid, src, tgt


def _artifacts(pid: str) -> dict[str, str]:
    store = get_artifact_store()
    with session_scope() as db:
        rows = db.query(GeneratedArtifact).filter(GeneratedArtifact.project_id == pid).all()
        return {r.relative_path: store.get_text(r.storage_uri) for r in rows}


@pytest.mark.parametrize("platform", ["snowflake", "databricks", "fabric"])
def test_converted_code_returns_same_rows_as_legacy(isolated_env: Path, platform: str) -> None:
    pid, src, tgt = _setup(isolated_env, platform)
    files = _artifacts(pid)
    sources = files["dbt/models/sources.yml"]
    legacy = duckdb.connect(str(src), read_only=True)
    target = duckdb.connect(str(tgt), read_only=True)
    try:
        expected = _norm(legacy.execute(EXPECTED_CURATED_ORDERS).fetchall())
        actual = _norm(
            _run_model(
                files["dbt/models/load_customers_and_orders__curated_orders.sql"],
                sources,
                platform,
                target,
            )
        )
        assert len(expected) == 96  # only orders over 100 pass the Conditional Split
        assert actual == expected

        expected_v = _norm(legacy.execute(EXPECTED_CUSTOMER_ORDERS).fetchall())
        actual_v = _norm(
            _run_model(files["dbt/models/v_customer_orders.sql"], sources, platform, target)
        )
        assert len(expected_v) == 20
        assert actual_v == expected_v
    finally:
        legacy.close()
        target.close()


def test_gaps_are_flagged_not_guessed(isolated_env: Path) -> None:
    pid, _, _ = _setup(isolated_env, "snowflake")
    with session_scope() as db:
        items = db.query(ResidueItem).filter(ResidueItem.project_id == pid).all()
        found = {(i.object_key, i.reason_code) for i in items}
    # Load_Finance_GL maps orders straight into gl_entries without a column mapping: the required
    # gl_entries columns cannot be filled, so they are flagged rather than invented.
    assert ("Load_Finance_GL", "TARGET_COLUMN_UNMAPPED") in found
    assert ("Load_Customers_And_Orders", "SSIS_SCRIPT_TASK") in found
    assert ("Load_Customers_And_Orders", "UNKNOWN_TABLE_REFERENCE") in found
    files = _artifacts(pid)
    assert not any("l.id = r.id" in c for c in files.values())  # no invented join keys


def test_approved_fixes_survive_reconvert(isolated_env: Path) -> None:
    pid, _, _ = _setup(isolated_env, "snowflake")
    with session_scope() as db:
        for item in (
            db.query(ResidueItem)
            .filter(ResidueItem.object_key == "Load_Customers_And_Orders")
            .all()
        ):
            assert item.component_key, "residues must carry a stable component id"
            ConvertEngine().decide_residue(
                db, pid, item.id, "approved", "", "Not needed", "rev", "reviewer"
            )
        ConvertEngine().run(db, pid)
        ConvertEngine().run(db, pid)
        reopened = (
            db.query(ResidueItem)
            .filter(
                ResidueItem.object_key == "Load_Customers_And_Orders", ResidueItem.status == "open"
            )
            .count()
        )
        status = (
            db.query(ObjectStatus)
            .filter_by(project_id=pid, object_key="Load_Customers_And_Orders")
            .one()
        )
    assert reopened == 0
    assert status.blocker_reason is None
    assert status.state == "Reviewed"


def test_changed_source_sends_object_back(isolated_env: Path) -> None:
    pid, _, _ = _setup(isolated_env, "snowflake")
    changed = isolated_env / "changed"
    changed.mkdir()
    pkg = (EXAMPLES / "ssis" / "Load_Finance_GL.dtsx").read_text(encoding="utf-8")
    (changed / "Load_Finance_GL.dtsx").write_text(
        pkg.replace("Dest_GL", "Dest_GL_v2"), encoding="utf-8"
    )
    with session_scope() as db:
        before = (
            db.query(ObjectStatus)
            .filter_by(project_id=pid, object_key="Load_Finance_GL")
            .one()
            .state
        )
        DiscoverEngine().run(db, pid, extra_etl_paths=[changed / "Load_Finance_GL.dtsx"])
        after = db.query(ObjectStatus).filter_by(project_id=pid, object_key="Load_Finance_GL").one()
        event = (
            db.query(StatusEvent)
            .filter_by(project_id=pid, object_key="Load_Finance_GL", evidence_type="source_changed")
            .one()
        )
    assert before == "Assessed" and after.state == "Discovered"
    assert event.from_state == "Assessed"


def test_reconcile_skips_tables_not_loaded(isolated_env: Path) -> None:
    raw = (EXAMPLES / "client_config.yaml").read_text(encoding="utf-8")
    spec = importlib.util.spec_from_file_location("seed_legacy_db", EXAMPLES / "seed_legacy_db.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    src, tgt = mod.seed_all(base_dir=isolated_env / "noload")
    get_vault().set_secret("vault://acme/sqlserver-ro", f"duckdb:///{src.as_posix()}")
    get_vault().set_secret("vault://acme/snowflake-deploy", f"duckdb:///{tgt.as_posix()}")
    with session_scope() as db:
        proj, _, _ = ensure_project_from_config(db, raw)
        DiscoverEngine().run(db, proj.id)
        AssessEngine().run(db, proj.id)
        res = ReconcileEngine().run(db, proj.id)
    assert res.skipped_not_loaded == 5 and res.tables_checked == 0 and res.failed == 0


def test_ssis_conditional_split_is_first_match(isolated_env: Path) -> None:
    """A row matching two outputs goes only to the first one, as in SSIS."""
    pkg = (EXAMPLES / "ssis" / "Load_Customers_And_Orders.dtsx").read_text(encoding="utf-8")
    two_routes = pkg.replace(
        '<output refId="split_default" name="Conditional Split Default Output" isDefaultOut="true"/>',
        '<output refId="split_mid" name="MidValueOrders"><properties>'
        '<property name="FriendlyExpression">gross_amount &gt; 50</property></properties></output>'
        '<output refId="split_default" name="Conditional Split Default Output" isDefaultOut="true"/>',
    ).replace('startId="split_out" endId="lkp_in"', 'startId="split_mid" endId="lkp_in"')
    reg = build_default_registry()
    ir = reg.get_parser("ssis").parse_artifact("x.dtsx", two_routes.encode("utf-8"))
    from accelerator.plugins.collectors.base_sql import parse_ddl_to_catalog

    catalog = parse_ddl_to_catalog(
        (EXAMPLES / "legacy_ddl.sql").read_text(), "sqlserver", "tsql", "legacy_ddl"
    )
    cfg = ClientConfig.model_validate(
        {
            "client": "t",
            "source": {"database": "sqlserver", "etl": "ssis"},
            "target": {"platform": "snowflake"},
        }
    )
    bundle = reg.get_generator("snowflake").generate_package(
        ir, {t.fqn: t for t in catalog.tables}, cfg
    )
    model = next(f.content for f in bundle.files if f.artifact_kind == "dbt_model")
    sources = next(f.content for f in bundle.files if f.relative_path.endswith("sources.yml"))

    spec = importlib.util.spec_from_file_location("seed_legacy_db", EXAMPLES / "seed_legacy_db.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    src, _ = mod.seed_all(base_dir=isolated_env / "split")
    conn = duckdb.connect(str(src))
    conn.execute("CREATE SCHEMA curated_legacy_ddl")
    for t in ("orders", "customers"):
        conn.execute(f"CREATE TABLE curated_legacy_ddl.{t.upper()} AS SELECT * FROM dbo.{t}")
    rows = _run_model(model, sources, "snowflake", conn)
    expected = conn.execute(
        "SELECT count(*) FROM dbo.orders WHERE gross_amount > 50 AND NOT gross_amount > 100"
    ).fetchone()[0]
    conn.close()
    assert len(rows) == expected and all(50 < float(r[3]) <= 100 for r in rows)


def test_custom_rule_grammar() -> None:
    rule = CustomRule(match="component_type == 'ScriptTask'")
    assert rule.matches("ScriptTask")
    assert rule.matches("Microsoft.ScriptTask")
    assert not rule.matches("")  # a blank type must not match
    assert not rule.matches("Task")
    both = CustomRule(match="component_type contains 'lookup' and object_key startswith 'load_'")
    assert both.matches("Microsoft.Lookup", object_key="Load_Customers")
    assert not both.matches("Microsoft.Lookup", object_key="other")
    with pytest.raises(ValueError):
        CustomRule(match="component_type = ScriptTask")
