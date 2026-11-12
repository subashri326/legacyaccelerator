"""Stage 3: live databases through the connector layer, restartable loads, incremental upserts,
cross-engine reconciliation.

PostgreSQL tests use a real server (L2C_TEST_POSTGRES_URL; CI provides one). They prove the
generic SQLAlchemy path that SQL Server, Snowflake, Databricks and Fabric also use.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import duckdb
import pytest
from sqlalchemy import create_engine, text

from accelerator.connectors.base import DuckDBConnector, SqlAlchemyConnector
from accelerator.core.secrets import get_vault
from accelerator.core.storage import get_artifact_store
from accelerator.db.models import LoadRun
from accelerator.db.repository import ensure_project_from_config
from accelerator.db.session import session_scope
from accelerator.engines.assess import AssessEngine
from accelerator.engines.convert import ConvertEngine
from accelerator.engines.discover import DiscoverEngine
from accelerator.engines.move import MoveEngine
from accelerator.engines.reconcile import ReconcileEngine
from accelerator.ir.models import ColumnIR, TableIR
from accelerator.plugins.movers.parquet_mover import _page_query

EXAMPLES = Path("examples/acme_retail")

PG_DDL = """
CREATE SCHEMA dbo; CREATE SCHEMA finance;
CREATE TABLE dbo.customers (customer_id INT PRIMARY KEY, full_name VARCHAR(120) NOT NULL, email VARCHAR(200),
  ssn VARCHAR(20), segment VARCHAR(40), created_at TIMESTAMP NOT NULL);
CREATE TABLE dbo.products (product_id INT PRIMARY KEY, sku VARCHAR(40) NOT NULL, product_name VARCHAR(150) NOT NULL,
  unit_price NUMERIC(12,2) NOT NULL);
CREATE TABLE dbo.orders (order_id INT PRIMARY KEY, customer_id INT NOT NULL REFERENCES dbo.customers(customer_id),
  product_id INT NOT NULL REFERENCES dbo.products(product_id), quantity INT NOT NULL, gross_amount NUMERIC(14,2) NOT NULL,
  discount NUMERIC(10,2), updated_at TIMESTAMP NOT NULL);
CREATE TABLE finance.gl_entries (entry_id INT PRIMARY KEY, order_id INT NOT NULL REFERENCES dbo.orders(order_id),
  account_code VARCHAR(20) NOT NULL, debit_amount NUMERIC(14,2) NOT NULL, credit_amount NUMERIC(14,2) NOT NULL,
  posted_at TIMESTAMP NOT NULL);
CREATE TABLE dbo.unused_temp_log (log_id INT PRIMARY KEY, message VARCHAR(500));
CREATE VIEW dbo.v_customer_orders AS SELECT c.customer_id, c.full_name, COUNT(o.order_id) AS order_count,
  SUM(o.gross_amount - COALESCE(o.discount, 0)) AS net_spend FROM dbo.customers c JOIN dbo.orders o
  ON c.customer_id = o.customer_id GROUP BY c.customer_id, c.full_name;
"""
TABLES = [
    "dbo.customers",
    "dbo.products",
    "dbo.orders",
    "finance.gl_entries",
    "dbo.unused_temp_log",
]


def _seed_duckdb(tmp: Path) -> tuple[Path, Path]:
    spec = importlib.util.spec_from_file_location("seed_legacy_db", EXAMPLES / "seed_legacy_db.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.seed_all(base_dir=tmp)


def _seed_postgres(url: str, tmp: Path) -> None:
    src, _ = _seed_duckdb(tmp / "pgseed")
    eng = create_engine(url)
    with eng.begin() as c:
        for stmt in PG_DDL.split(";"):
            if stmt.strip():
                c.execute(text(stmt))
    d = duckdb.connect(str(src), read_only=True)
    with eng.begin() as c:
        for t in TABLES:
            rows = d.execute(f"SELECT * FROM {t}").fetchall()
            cols = [r[0] for r in d.execute(f"DESCRIBE {t}").fetchall()]
            c.execute(
                text(
                    f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join(':' + x for x in cols)})"
                ),
                [dict(zip(cols, r, strict=True)) for r in rows],
            )
    d.close()
    eng.dispose()


def _config(
    src_ref: str, tgt_ref: str, database: str, extra: str = "", bundle: bool = False
) -> str:
    raw = (EXAMPLES / "client_config.yaml").read_text(encoding="utf-8")
    raw = raw.replace("vault://acme/sqlserver-ro", src_ref).replace(
        "vault://acme/snowflake-deploy", tgt_ref
    )
    raw = raw.replace("  database: sqlserver\n", f"  database: {database}\n")
    if not bundle:
        raw = raw.replace("  catalog_bundle_path: examples/acme_retail/legacy_ddl.sql\n", "")
    return raw + extra


def _pipeline(raw: str, convert: bool = True) -> str:
    with session_scope() as db:
        proj, _, _ = ensure_project_from_config(db, raw)
        DiscoverEngine().run(db, proj.id)
        AssessEngine().run(db, proj.id)
        if convert:
            ConvertEngine().run(db, proj.id)
        return proj.id


def test_live_postgres_source_end_to_end(isolated_env: Path, pg_database: str) -> None:
    _seed_postgres(pg_database, isolated_env)
    _, tgt = _seed_duckdb(isolated_env / "tgt")
    get_vault().set_secret("vault://pg/src", pg_database)
    get_vault().set_secret("vault://pg/tgt", f"duckdb:///{tgt.as_posix()}")
    pid = _pipeline(_config("vault://pg/src", "vault://pg/tgt", "postgres"))

    from accelerator.db.models import DbColumn, DbObject

    with session_scope() as db:
        objs = {o.fqn: o for o in db.query(DbObject).filter_by(project_id=pid)}
        dbname = pg_database.rsplit("/", 1)[-1].split("?")[0]
        orders = objs[f"{dbname}.dbo.orders"]
        cols = {c.column_name: c for c in db.query(DbColumn).filter_by(db_object_id=orders.id)}
        assert cols["order_id"].is_primary_key
        assert cols["customer_id"].is_foreign_key and "customers" in (
            cols["customer_id"].fk_target or ""
        )
        assert orders.row_count == 120
        assert f"{dbname}.dbo.v_customer_orders" in objs  # live view harvested

        move = MoveEngine().run(db, pid)
        assert move.failed_tables == [] and move.total_rows_loaded == 271
        rec = ReconcileEngine().run(db, pid)
        assert rec.passed + rec.passed_within_tolerance == 5 and rec.failed == 0


def test_postgres_as_target(isolated_env: Path, pg_database: str) -> None:
    src, _ = _seed_duckdb(isolated_env / "src")
    get_vault().set_secret("vault://pgt/src", f"duckdb:///{src.as_posix()}")
    get_vault().set_secret("vault://pgt/tgt", pg_database)
    pid = _pipeline(_config("vault://pgt/src", "vault://pgt/tgt", "sqlserver", bundle=True))
    with session_scope() as db:
        move = MoveEngine().run(db, pid)
        assert move.failed_tables == [] and move.total_rows_loaded == 271
        rec = ReconcileEngine().run(db, pid)
        assert rec.failed == 0 and rec.tables_checked == 5
        run = db.query(LoadRun).filter_by(project_id=pid, object_key="legacy_ddl.dbo.orders").one()
        evidence = get_artifact_store().get_text(run.evidence_uri)
    assert '"target_ddl_executed": true' in evidence and "keyset on order_id" in evidence
    # Converted DDL decided the types: gross_amount is NUMERIC(14,2), not a guessed float.
    with create_engine(pg_database).connect() as c:
        dtype = c.execute(
            text(
                "SELECT data_type, numeric_precision, numeric_scale FROM information_schema.columns "
                "WHERE table_schema = 'curated_legacy_ddl' AND table_name = 'orders' AND column_name = 'gross_amount'"
            )
        ).one()
    assert tuple(dtype) == ("numeric", 14, 2)


def _local(isolated_env: Path, extra: str = "") -> tuple[str, Path, Path]:
    src, tgt = _seed_duckdb(isolated_env / "local")
    get_vault().set_secret("vault://loc/src", f"duckdb:///{src.as_posix()}")
    get_vault().set_secret("vault://loc/tgt", f"duckdb:///{tgt.as_posix()}")
    return (
        _pipeline(_config("vault://loc/src", "vault://loc/tgt", "sqlserver", extra, bundle=True)),
        src,
        tgt,
    )


def test_crash_mid_load_resumes_without_duplicates(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, _, tgt = _local(isolated_env)
    real_write = DuckDBConnector.write_arrow
    calls = {"n": 0}

    def flaky(self, data, schema, name):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise ConnectionError("network dropped")
        return real_write(self, data, schema, name)

    monkeypatch.setattr(DuckDBConnector, "write_arrow", flaky)
    with session_scope() as db:
        m1 = MoveEngine().run(db, pid, object_key="legacy_ddl.dbo.orders")
    assert m1.failed_tables == ["legacy_ddl.dbo.orders"]
    with session_scope() as db:
        run = db.get(LoadRun, m1.load_run_ids[0])
        assert run.status == "failed" and len(run.partitions) == 1 and run.rows_loaded == 50
    monkeypatch.setattr(DuckDBConnector, "write_arrow", real_write)

    with session_scope() as db:
        m2 = MoveEngine().run(
            db, pid, object_key="legacy_ddl.dbo.orders", resume_load_run_id=run.id
        )
        assert m2.failed_tables == [] and m2.total_rows_loaded == 70  # only what was missing
        run = db.get(LoadRun, run.id)
        assert run.status == "completed" and run.rows_loaded == 120
        assert [p.partition_index for p in run.partitions] == [0, 1, 2]
    c = duckdb.connect(str(tgt), read_only=True)
    total, distinct = c.execute(
        "SELECT count(*), count(DISTINCT order_id) FROM CURATED_LEGACY_DDL.ORDERS"
    ).fetchone()
    c.close()
    assert total == distinct == 120


def test_incremental_upserts_changed_rows(isolated_env: Path) -> None:
    extra = ""  # client_config already sets dbo.orders: updated_at as the watermark
    pid, src, tgt = _local(isolated_env, extra)
    with session_scope() as db:
        MoveEngine().run(db, pid, object_key="legacy_ddl.dbo.orders")
    s = duckdb.connect(str(src))
    s.execute(
        "UPDATE dbo.orders SET gross_amount = 999.99, updated_at = TIMESTAMP '2026-06-01 00:00:00' WHERE order_id = 5"
    )
    s.execute(
        "INSERT INTO dbo.orders VALUES (121, 1, 1, 1, 10.00, 0, TIMESTAMP '2026-06-02 00:00:00')"
    )
    s.close()
    with session_scope() as db:
        m = MoveEngine().run(db, pid, object_key="legacy_ddl.dbo.orders", mode="incremental")
        assert m.failed_tables == [] and m.total_rows_loaded == 2
        run = db.get(LoadRun, m.load_run_ids[0])
        assert run.watermark_value and run.watermark_value.startswith("2026-06-02")
    c = duckdb.connect(str(tgt), read_only=True)
    total, distinct = c.execute(
        "SELECT count(*), count(DISTINCT order_id) FROM CURATED_LEGACY_DDL.ORDERS"
    ).fetchone()
    amount = c.execute(
        "SELECT gross_amount FROM CURATED_LEGACY_DDL.ORDERS WHERE order_id = 5"
    ).fetchone()[0]
    c.close()
    assert total == distinct == 121 and float(amount) == 999.99


def test_incremental_without_watermark_is_refused(isolated_env: Path) -> None:
    pid, _, _ = _local(isolated_env)
    with session_scope() as db:
        m = MoveEngine().run(db, pid, object_key="legacy_ddl.dbo.customers", mode="incremental")
        run = db.get(LoadRun, m.load_run_ids[0])
        evidence = get_artifact_store().get_text(run.evidence_uri)
    assert m.failed_tables == ["legacy_ddl.dbo.customers"] and "watermark" in evidence


def test_reconcile_tolerance_and_masked_mismatch(isolated_env: Path) -> None:
    extra = ""
    pid, _, tgt = _local(isolated_env, extra)
    with session_scope() as db:
        MoveEngine().run(db, pid)
    c = duckdb.connect(str(tgt))
    c.execute(
        "UPDATE CURATED_LEGACY_DDL.ORDERS SET gross_amount = gross_amount + 0.01 WHERE order_id = 7"
    )
    c.close()
    with session_scope() as db:
        # orders: default L2 with 0.01 tolerance -> a 0.01 drift passes within tolerance
        r = ReconcileEngine().run(db, pid, object_key="legacy_ddl.dbo.orders")
        assert r.passed_within_tolerance == 1 and r.failed == 0
    c = duckdb.connect(str(tgt))
    c.execute("UPDATE CURATED_LEGACY_DDL.CUSTOMERS SET email = 'x@y.z' WHERE customer_id = 3")
    c.close()
    from accelerator.db.models import ReconResult

    with session_scope() as db:
        r = ReconcileEngine().run(
            db, pid, object_key="legacy_ddl.dbo.customers", level_override="L3"
        )
        assert r.failed == 1
        res = (
            db.query(ReconResult)
            .filter_by(object_key="legacy_ddl.dbo.customers")
            .order_by(ReconResult.created_at.desc())
            .first()
        )
        diff = res.diff_summary
    assert diff["bucket_mismatches"] and list(diff["bucket_mismatches"]) == ["3"]
    sample = diff["l4_diff"]["value_mismatch_sample"][0]
    assert (
        sample["key"] == 3.0
        and "cust3@acme.example" not in str(sample)
        and "x@y.z" not in str(sample)
    )


def test_sql_rendering_for_each_engine() -> None:
    table = TableIR(
        schema_name="dbo",
        name="orders",
        columns=[
            ColumnIR(name="order_id", data_type="INT", is_primary_key=True),
            ColumnIR(name="order", data_type="VARCHAR(5)"),
        ],
    )
    rendered = {}
    for dialect in ("tsql", "snowflake", "databricks", "postgres"):
        conn = SqlAlchemyConnector.__new__(SqlAlchemyConnector)
        conn.dialect = dialect
        rendered[dialect] = _page_query(conn, table, "order_id", 100, None, None, 50, 0)
    assert rendered["tsql"].startswith("SELECT TOP 50") and "[order]" in rendered["tsql"]
    assert "LIMIT 50" in rendered["snowflake"] and '"order"' in rendered["snowflake"]
    assert "`order`" in rendered["databricks"] and "order_id > 100" in rendered["databricks"]
    ddl = "CREATE TABLE IF NOT EXISTS CURATED_X.ORDERS (order_id NUMBER(38,0) NOT NULL, ts TIMESTAMP_NTZ)"
    pg = SqlAlchemyConnector.__new__(SqlAlchemyConnector)
    pg.dialect = "postgres"
    assert pg.transpile(ddl, read="snowflake") == [
        "CREATE TABLE IF NOT EXISTS CURATED_X.ORDERS (order_id DECIMAL(38, 0) NOT NULL, ts TIMESTAMP)"
    ]
