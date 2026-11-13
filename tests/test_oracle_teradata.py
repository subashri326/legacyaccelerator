"""Oracle and Teradata: catalog exports, type mapping, SQL conversion, loading and reconciliation.

The conversion proofs follow test_conversion_correctness: each example runs Discover -> Assess
-> Convert -> Move with a DuckDB stand-in holding the legacy data; every generated model is run
on the migrated data and compared with a hand-written query of what the legacy SQL returns under
Oracle's / Teradata's own rules.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
import sqlglot
from legacy_db_pipeline import run_pipeline
from test_conversion_correctness import _artifacts, _run_model

from accelerator.connectors.base import DuckDBConnector
from accelerator.db.models import ObjectStatus, ResidueItem
from accelerator.db.session import session_scope
from accelerator.ir.models import ColumnIR, TableIR
from accelerator.plugins.collectors.ddl_reader import read_ddl_export
from accelerator.plugins.generators.types import map_source_type
from accelerator.plugins.movers.parquet_mover import _page_query
from accelerator.plugins.validators.sql_validator import _kind, _profile

ORACLE = Path("examples/oracle_sales")
TERADATA = Path("examples/teradata_edw")


def _norm(rows: list[tuple]) -> list[tuple]:
    def v(x: Any) -> Any:
        if isinstance(x, (Decimal, float)) or (isinstance(x, int) and not isinstance(x, bool)):
            return round(float(x), 4)
        if isinstance(x, datetime) and x.time() == datetime.min.time():
            return x.date()  # DATE + n days: a date on some engines, midnight on others
        return x

    return sorted((tuple(v(x) for x in r) for r in rows), key=repr)


# --- reading the exports -----------------------------------------------------------------------
def test_oracle_export_is_read_completely() -> None:
    cat = read_ddl_export((ORACLE / "oracle_export.sql").read_text(), "oracle", "oracle", "ORCL")
    tables = {t.name: t for t in cat.tables}
    assert set(tables) == {"CUSTOMERS", "ORDERS"}
    orders = {c.name: c for c in tables["ORDERS"].columns}
    assert orders["ORDER_ID"].is_primary_key  # from the separate ALTER TABLE ... PRIMARY KEY
    assert orders["CUSTOMER_ID"].fk_target == "SALES.CUSTOMERS"
    assert orders["ORDER_DATE"].data_type == "DATE" and not orders["ORDER_DATE"].nullable
    assert orders["UPDATED_TS"].data_type == "TIMESTAMP(6) WITH TIME ZONE"
    assert orders["NOTE"].attributes == {"unicode": True}  # VARCHAR2(400 CHAR)
    assert orders["AMOUNT"].precision == 12 and orders["AMOUNT"].scale == 2
    assert {v.name for v in cat.views} == {
        "V_ORDER_SUMMARY",
        "V_CUSTOMER_ACTIVITY",
        "V_TOP_ORDERS",
        "V_RECENT_ORDERS",
    }
    assert [p.name for p in cat.procedures] == ["P_ARCHIVE_ORDERS"]
    assert cat.procedures[0].definition_sql.lstrip().startswith("CREATE OR REPLACE")


def test_teradata_export_is_read_completely() -> None:
    cat = read_ddl_export((TERADATA / "teradata_export.sql").read_text(), "teradata", "teradata",
                          "TD")  # fmt: skip
    tables = {t.name: t for t in cat.tables}
    assert set(tables) == {"CUSTOMER_DIM", "SALES_FACT", "SALES_MART"}
    cust = {c.name: c for c in tables["CUSTOMER_DIM"].columns}
    # a unique primary index on a NOT NULL column is the row key...
    assert tables["CUSTOMER_DIM"].attributes == {"key_source": "UNIQUE PRIMARY INDEX"}
    assert cust["CUST_ID"].is_primary_key
    # ...a non-unique primary index is only how rows are spread, not a key
    assert not any(c.is_primary_key for c in tables["SALES_FACT"].columns)
    assert cust["CUST_NM"].attributes.get("case_insensitive") is True
    assert "case_insensitive" not in cust["SEG_CD"].attributes  # CASESPECIFIC
    assert cust["CITY_NM"].attributes == {"unicode": True, "case_insensitive": True}
    assert cust["BIRTH_DT"].attributes.get("format") == "YYYY-MM-DD"
    assert {v.name for v in cat.views} == {"V_SALES_SUMMARY", "V_CITY_COUNTS", "V_SAMPLE_SALES"}
    assert [p.name for p in cat.procedures] == ["SP_REBUILD_MART"]


@pytest.mark.parametrize(
    ("db", "src", "attrs", "platform", "expected", "noted"),
    [
        ("oracle", "DATE", {}, "snowflake", "TIMESTAMP_NTZ(0)", False),  # Oracle DATE has a time
        ("oracle", "DATE", {}, "fabric", "DATETIME2(0)", False),
        ("oracle", "NUMBER(12,2)", {}, "databricks", "DECIMAL(12,2)", False),
        ("oracle", "NUMBER(10,0)", {}, "snowflake", "NUMBER(10,0)", False),
        ("oracle", "NUMBER", {}, "snowflake", "NUMBER(38,10)", True),
        ("oracle", "VARCHAR2(100)", {"unicode": True}, "fabric", "VARCHAR(400)", False),
        ("oracle", "VARCHAR2(100)", {}, "fabric", "VARCHAR(100)", False),
        ("oracle", "TIMESTAMP(6) WITH TIME ZONE", {}, "snowflake", "TIMESTAMP_TZ(6)", False),
        ("oracle", "TIMESTAMP(6) WITH TIME ZONE", {}, "fabric", "DATETIME2(6)", True),
        ("oracle", "CLOB", {}, "fabric", "VARCHAR(MAX)", False),
        ("oracle", "RAW(16)", {}, "databricks", "BINARY", False),
        ("oracle", "XMLTYPE", {}, "snowflake", "VARCHAR", True),
        ("teradata", "BYTEINT", {}, "fabric", "SMALLINT", False),
        ("teradata", "BYTEINT", {}, "databricks", "TINYINT", False),
        ("teradata", "DATE", {}, "snowflake", "DATE", False),
        ("teradata", "TIME(6)", {}, "databricks", "STRING", True),
        ("teradata", "VARCHAR(60)", {"unicode": True}, "fabric", "VARCHAR(240)", False),
        ("teradata", "PERIOD(DATE)", {}, "snowflake", "VARCHAR", True),
        ("teradata", "DECIMAL(18,2)", {}, "snowflake", "NUMBER(18,2)", False),
    ],
)
def test_types_are_mapped_by_meaning(
    db: str, src: str, attrs: dict, platform: str, expected: str, noted: bool
) -> None:
    target, note = map_source_type(src, db, platform, attrs)
    assert target == expected
    assert bool(note) is noted


# --- converted SQL returns the same rows ------------------------------------------------------
ORACLE_EXPECTED = {
    # '' is NULL in Oracle (so 'EMPTY' never shows); || skips NULL parts; DECODE matches NULL;
    # date + 30 adds days; TRUNC(date) is midnight; SUBSTR from 0 starts at 1.
    "v_order_summary": """SELECT o.order_id,
        c.full_name || ' (' || COALESCE(c.segment, '') || ')',
        date_trunc('day', o.order_date), o.order_date + INTERVAL 30 DAY,
        CASE WHEN o.status = 'N' THEN 'New' WHEN o.status = 'S' THEN 'Shipped'
             WHEN o.status IS NULL THEN 'Unknown' ELSE 'Other' END,
        o.amount - COALESCE(o.discount, 0),
        CASE WHEN o.note IS NULL THEN 'NONE' ELSE 'TEXT' END,
        SUBSTRING(c.email, 1, 3)
        FROM sales.orders o JOIN sales.customers c ON c.customer_id = o.customer_id""",
    # (+) is a left outer join; customers without orders count 0.
    "v_customer_activity": """SELECT c.customer_id, c.full_name, COUNT(o.order_id),
        MAX(strftime(o.order_date, '%Y-%m'))
        FROM sales.customers c LEFT JOIN sales.orders o ON c.customer_id = o.customer_id
        GROUP BY c.customer_id, c.full_name""",
}
TERADATA_EXPECTED = {
    # NOT CASESPECIFIC: LIKE 'customer%' matches 'Customer 1'; INTEGER / INTEGER drops the
    # remainder; DATE + 7 adds days; QUALIFY keeps the latest 3 sales per customer.
    "v_sales_summary": """SELECT sale_id, cust_id, cust_nm, amt, boxes, due_dt, qty_or_null FROM (
        SELECT f.sale_id, f.cust_id, c.cust_nm, COALESCE(f.amt, 0) AS amt,
               f.qty // f.units_per_box AS boxes, f.sale_dt + 7 AS due_dt,
               NULLIF(f.qty, 0) AS qty_or_null,
               ROW_NUMBER() OVER (PARTITION BY f.cust_id ORDER BY f.sale_id DESC) AS rn
        FROM edw.sales_fact f JOIN edw.customer_dim c ON f.cust_id = c.cust_id
        WHERE upper(c.cust_nm) LIKE 'CUSTOMER%') x WHERE rn <= 3""",
    # BTEQ: INS ... SEL ... GROUP BY 1
    "load_sales_mart__sales_mart": """SELECT cust_id, SUM(COALESCE(amt, 0)), COUNT(*)
        FROM edw.sales_fact GROUP BY cust_id""",
}


def _check(example: Path, expected: dict[str, str], tmp: Path, platform: str) -> str:
    pid, src, tgt = run_pipeline(tmp, example, platform)
    files = _artifacts(pid)
    sources = files["dbt/models/sources.yml"]
    legacy = duckdb.connect(str(src), read_only=True)
    target = duckdb.connect(str(tgt), read_only=True)
    try:
        for name, sql in expected.items():
            want = _norm(legacy.execute(sql).fetchall())
            got = _norm(_run_model(files[f"dbt/models/{name}.sql"], sources, platform, target))
            assert got == want, f"{platform}: {name} differs"
            assert want, name
    finally:
        legacy.close()
        target.close()
    return pid


@pytest.mark.parametrize("platform", ["snowflake", "databricks", "fabric"])
def test_oracle_views_return_same_rows(isolated_env: Path, platform: str) -> None:
    _check(ORACLE, ORACLE_EXPECTED, isolated_env, platform)


@pytest.mark.parametrize("platform", ["snowflake", "databricks", "fabric"])
def test_teradata_views_and_bteq_return_same_rows(isolated_env: Path, platform: str) -> None:
    pid = _check(TERADATA, TERADATA_EXPECTED, isolated_env, platform)
    with duckdb.connect(str(isolated_env / f"teradata_edw_{platform}" / "teradata_edw.duckdb"),
                        read_only=True) as legacy:  # fmt: skip
        mixed = legacy.execute("SELECT COUNT(*) FROM edw.sales_fact WHERE qty % units_per_box <> 0"
                               ).fetchone()  # fmt: skip
    assert mixed and mixed[0] > 0  # integer division really drops a remainder in the data
    assert pid


def test_what_cannot_be_exact_is_flagged(isolated_env: Path) -> None:
    found: set[tuple[str, str]] = set()
    for example in (ORACLE, TERADATA):
        pid, _, _ = run_pipeline(isolated_env, example, "snowflake")
        with session_scope() as db:
            found |= {(i.object_key.rsplit(".", 1)[-1], i.reason_code) for i in
                      db.query(ResidueItem).filter(ResidueItem.project_id == pid,
                                                   ResidueItem.status == "open")}  # fmt: skip
    assert ("V_TOP_ORDERS", "ORACLE_ROWNUM") in found
    assert ("V_RECENT_ORDERS", "ORACLE_NLS_DATE_LITERAL") in found
    assert ("P_ARCHIVE_ORDERS", "PROCEDURAL_BLOCK") in found
    assert ("V_CITY_COUNTS", "TD_CASE_INSENSITIVE_GROUPING") in found
    assert ("V_SAMPLE_SALES", "TD_SAMPLE") in found
    assert ("SP_REBUILD_MART", "PROCEDURAL_BLOCK") in found
    assert ("load_sales_mart", "BTEQ_COMMAND") in found  # .EXPORT; .IF ERRORCODE is not flagged
    converted = {"V_ORDER_SUMMARY", "V_CUSTOMER_ACTIVITY", "V_SALES_SUMMARY"}
    assert not {name for name, _ in found} & converted, found


def test_inexact_type_mapping_holds_the_table_until_approved(isolated_env: Path) -> None:
    pid, _, _ = run_pipeline(isolated_env, ORACLE, "fabric", approve_type_reviews=False)
    with session_scope() as db:
        items = {
            i.component_name: i.reason_detail
            for i in db.query(ResidueItem).filter(
                ResidueItem.project_id == pid, ResidueItem.reason_code == "TYPE_MAPPING_REVIEW")
        }  # fmt: skip
        states = {o.object_key.rsplit(".", 1)[-1]: o.state for o in db.query(ObjectStatus)
                  .filter(ObjectStatus.project_id == pid)}  # fmt: skip
    assert set(items) == {"CUSTOMERS.CREDIT_LIMIT", "ORDERS.UPDATED_TS"}
    assert "UTC" in items["ORDERS.UPDATED_TS"]
    assert states["CUSTOMERS"] != "Loaded" and states["ORDERS"] != "Loaded"


# --- loading and reconciliation SQL in the source's own dialect -------------------------------
class _Speaks(DuckDBConnector):
    """A DuckDB database that accepts SQL written in another dialect (to test generated SQL)."""

    def __init__(self, path: str, dialect: str) -> None:
        super().__init__(path, read_only=True)
        self.dialect = dialect

    def _tr(self, sql: str) -> str:
        return sqlglot.transpile(sql, read=self.dialect, write="duckdb")[0]

    def query(self, sql: str, params: Any = None) -> list[tuple[Any, ...]]:
        return super().query(self._tr(sql), params)


@pytest.mark.parametrize("dialect", ["oracle", "teradata"])
def test_paging_and_reconciliation_sql_work_in_the_source_dialect(
    tmp_path: Path, dialect: str
) -> None:
    db = tmp_path / "src.duckdb"
    with duckdb.connect(str(db)) as c:
        c.execute("CREATE SCHEMA s")
        c.execute("CREATE TABLE s.t (id INTEGER, name VARCHAR, amt DECIMAL(10,2))")
        c.execute("INSERT INTO s.t SELECT i, 'n' || (i % 7), i * 1.5 FROM range(1, 54) r(i)")
    table = TableIR(schema_name="s", name="t", columns=[
        ColumnIR(name="id", data_type="INTEGER", is_primary_key=True, nullable=False),
        ColumnIR(name="name", data_type="VARCHAR(10)"),
        ColumnIR(name="amt", data_type="DECIMAL(10,2)"),
    ])  # fmt: skip
    src = _Speaks(str(db), dialect)
    plain = DuckDBConnector(str(db), read_only=True)
    try:
        everything = sorted(plain.query("SELECT id, name, amt FROM s.t"))
        # keyset pages (by key) and offset pages (no key) each return every row exactly once
        for key in ("id", None):
            rows, after, offset = [], None, 0
            while True:
                sql = _page_query(src, table, key, after, None, None, 10, offset)
                page = src.query(sql)
                if not page:
                    break
                rows += page
                after, offset = page[-1][0], offset + len(page)
            assert sorted(rows) == everything, (dialect, key)
        kinds = [_kind(c.data_type) for c in table.columns]
        assert _profile(src, table, kinds, "L3", "id") == _profile(plain, table, kinds, "L3", "id")
    finally:
        src.close()
        plain.close()
