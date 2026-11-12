"""Tests for T-SQL/PL-SQL/BTEQ parsers, vendor wrappers, incremental watermark loads, job-level recon, and Alembic."""

from __future__ import annotations

from pathlib import Path

import duckdb
from alembic import command
from alembic.config import Config

from accelerator.config.client_config import ClientConfig, SourceConfig, TargetConfig
from accelerator.core.secrets import get_vault
from accelerator.db.models import LoadRun
from accelerator.db.repository import ensure_project_from_config
from accelerator.db.session import session_scope
from accelerator.engines.assess import AssessEngine
from accelerator.engines.discover import DiscoverEngine
from accelerator.engines.move import MoveEngine
from accelerator.ir.models import PackageIR
from accelerator.plugins.registry import build_default_registry


def test_sql_parsers_and_vendor_wrappers() -> None:
    reg = build_default_registry()

    bteq_script = b"""
    .LOGON tdprod/user,pwd;
    INSERT INTO edw.cust_dim SELECT cust_id, cust_name FROM stg.cust WHERE active_flag = 1;
    .IF ERRORCODE <> 0 THEN .QUIT 8;
    .LOGOFF;
    """
    bteq_pkg = reg.get_parser("bteq").parse_artifact("load_cust.bteq", bteq_script)
    assert len(bteq_pkg.data_flows) == 1
    assert any(o.reason_code == "BTEQ_CONTROL_FLOW" for o in bteq_pkg.opaque_items)

    plsql_script = b"""
    INSERT INTO finance.summary SELECT acct_id, SUM(amt) FROM finance.txns GROUP BY acct_id;
    EXECUTE IMMEDIATE 'TRUNCATE TABLE finance.tmp';
    """
    pl_pkg = reg.get_parser("plsql").parse_artifact("agg_fin.sql", plsql_script)
    assert any(o.reason_code == "PROCEDURAL_OR_DYNAMIC_SQL" for o in pl_pkg.opaque_items)

    cfg = ClientConfig(
        client="test_wrap",
        source=SourceConfig(database="oracle", etl="sql"),
        target=TargetConfig(platform="databricks", output="dbt"),
    )
    bundle = reg.get_generator("lakebridge_wrapper").generate_package(bteq_pkg, {}, cfg)
    assert any(
        "Databricks Lakebridge" in f.content for f in bundle.files if f.artifact_kind == "dbt_model"
    )


def test_incremental_watermark_move_and_job_reconcile(isolated_env: Path) -> None:
    src_db = isolated_env / "src_inc.duckdb"
    tgt_db = isolated_env / "tgt_inc.duckdb"

    s_conn = duckdb.connect(str(src_db))
    s_conn.execute("CREATE SCHEMA dbo;")
    s_conn.execute(
        "CREATE TABLE dbo.events (event_id INT PRIMARY KEY, amount DOUBLE, updated_at VARCHAR);"
    )
    s_conn.execute(
        "INSERT INTO dbo.events VALUES (1, 10.0, '2026-01-01'), (2, 20.0, '2026-01-02');"
    )
    s_conn.close()
    duckdb.connect(str(tgt_db)).close()

    vault = get_vault()
    vault.set_secret("vault://inc/src", f"duckdb:///{src_db.as_posix()}")
    vault.set_secret("vault://inc/tgt", f"duckdb:///{tgt_db.as_posix()}")

    yaml_cfg = """
client: inc_client
source:
  database: sqlserver
  etl: none
  connection: vault://inc/src
target:
  platform: snowflake
  output: sql
  connection: vault://inc/tgt
movement:
  mover: parquet
  partition_size_rows: 10
  watermark_columns:
    src_inc.dbo.events: updated_at
"""
    with session_scope() as db:
        proj, _, _ = ensure_project_from_config(db, yaml_cfg)
        DiscoverEngine().run(db, proj.id)
        AssessEngine().run(db, proj.id)
        m1 = MoveEngine().run(db, proj.id, mode="bulk")
        assert m1.total_rows_loaded == 2

    # Add 1 new watermark row to source and run incremental move
    s_conn = duckdb.connect(str(src_db))
    s_conn.execute("INSERT INTO dbo.events VALUES (3, 35.0, '2026-01-03');")
    s_conn.close()

    with session_scope() as db:
        m2 = MoveEngine().run(db, proj.id, mode="incremental")
        assert m2.total_rows_loaded == 1
        latest_run = db.query(LoadRun).filter(LoadRun.id == m2.load_run_ids[0]).first()
        assert latest_run is not None
        assert latest_run.watermark_value == "2026-01-03"

    # Job-level reconciliation check on identical query outputs
    reg = build_default_registry()
    val = reg.get_validator("snowflake")
    out = val.reconcile_job(
        package=PackageIR(id="job1", name="job1", source_tool="tsql", source_artifact_name="j.sql"),
        legacy_sql="SELECT COUNT(*), SUM(amount) FROM dbo.events",
        target_sql="SELECT COUNT(*), SUM(amount) FROM dbo.events",
        source_uri="vault://inc/src",
        target_uri="vault://inc/tgt",
    )
    assert out.verdict == "pass"


def test_alembic_upgrade_head(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    alembic_db = tmp_path / "alembic_test.db"
    monkeypatch.setenv("L2C_DATABASE_URL", f"sqlite:///{alembic_db.as_posix()}")
    from accelerator.config.settings import get_settings

    get_settings.cache_clear()
    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")
    assert alembic_db.exists()
    get_settings.cache_clear()
