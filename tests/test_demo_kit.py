"""Stage 6c: what the live Oracle / SQL Server -> Snowflake / Databricks demo depends on.

The real databases run in Docker on the demo laptop; these tests cover everything that does not
need them: how live type names are read and mapped, Oracle stored code, the Databricks writer, the
Snowflake key-file sign-in, the SQL Server driver choice, and the demo data itself.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
import sqlglot

from accelerator.connectors import base as connectors
from accelerator.connectors.catalog import CATALOG, PYMSSQL
from accelerator.plugins.collectors.base_sql import _ident, _oracle_procedures, _type_text
from accelerator.plugins.generators.types import read_oracle, read_sqlserver, write

ROOT = Path(__file__).resolve().parents[1]


def _mapped(reader: Any, t: str, platform: str) -> str:
    return write(reader(t), platform)[0]


# ---------------------------------------------------------------- types as live databases report
def test_live_oracle_type_names_keep_precision_and_character_lengths() -> None:
    from sqlalchemy.dialects import oracle

    d = oracle.dialect()
    assert _type_text(oracle.NUMBER(10, 0), d) == "NUMBER(10, 0)"
    assert _type_text(oracle.VARCHAR2(120), d) == "VARCHAR2(120 CHAR)"
    assert _type_text(oracle.TIMESTAMP(timezone=True), d) == "TIMESTAMP WITH TIME ZONE"
    assert _mapped(read_oracle, "NUMBER(10, 0)", "snowflake") == "NUMBER(10,0)"
    assert _mapped(read_oracle, "VARCHAR2(120 CHAR)", "snowflake") == "VARCHAR(120)"
    assert _mapped(read_oracle, "VARCHAR2(120 CHAR)", "fabric") == "VARCHAR(480)"  # bytes
    assert _mapped(read_oracle, "VARCHAR2(120 BYTE)", "fabric") == "VARCHAR(120)"
    assert _mapped(read_oracle, "TIMESTAMP WITH TIME ZONE", "snowflake") == "TIMESTAMP_TZ(6)"
    assert _mapped(read_oracle, "DATE", "snowflake") == "TIMESTAMP_NTZ(0)"


def test_sql_server_types_map_to_valid_targets() -> None:
    from sqlalchemy import types
    from sqlalchemy.dialects import mssql

    d = mssql.dialect()
    assert _type_text(mssql.TIMESTAMP(), d) == "ROWVERSION"  # a row counter, not a time
    assert (
        _type_text(types.VARCHAR(200, collation="SQL_Latin1_General_CP1_CI_AS"), d)
        == "VARCHAR(200) COLLATE SQL_Latin1_General_CP1_CI_AS"
    )
    cases = {
        "INTEGER": ("NUMBER(10,0)", "INT"),
        "TINYINT": ("NUMBER(5,0)", "SMALLINT"),  # 0..255 does not fit a signed byte
        "BIT": ("BOOLEAN", "BOOLEAN"),
        "MONEY": ("NUMBER(19,4)", "DECIMAL(19,4)"),
        "DECIMAL(14, 2)": ("NUMBER(14,2)", "DECIMAL(14,2)"),
        "NVARCHAR(120)": ("VARCHAR(120)", "STRING"),
        "VARCHAR(200) COLLATE SQL_Latin1_General_CP1_CI_AS": ("VARCHAR(200)", "STRING"),
        "NVARCHAR(max)": ("VARCHAR", "STRING"),
        "VARBINARY(max)": ("BINARY", "BINARY"),
        "UNIQUEIDENTIFIER": ("VARCHAR(36)", "STRING"),
        "DATETIME2": ("TIMESTAMP_NTZ(7)", "TIMESTAMP_NTZ"),
        "DATETIME": ("TIMESTAMP_NTZ(3)", "TIMESTAMP_NTZ"),
        "DATETIMEOFFSET": ("TIMESTAMP_TZ(7)", "TIMESTAMP"),
        "DATE": ("DATE", "DATE"),
    }
    for src, (sf, dbx) in cases.items():
        assert _mapped(read_sqlserver, src, "snowflake") == sf, src
        assert _mapped(read_sqlserver, src, "databricks") == dbx, src
    assert read_sqlserver("ROWVERSION").note
    assert read_sqlserver("DATETIMEOFFSET").kind == "timestamptz"
    assert write(read_sqlserver("TIME(7)"), "databricks")[1]  # no TIME type: flagged


def test_names_stay_unquoted_so_oracle_upper_cases_them() -> None:
    assert _ident("oracle", "orders") == "orders"
    assert _ident("oracle", "order lines") == '"order lines"'
    assert _ident("mssql", "order lines") == "[order lines]"


def test_oracle_procedures_are_rebuilt_from_all_source() -> None:
    class Result:
        def fetchall(self) -> list[tuple[str, str, int, str]]:
            return [
                ("SALES", "P_ARCHIVE_ORDERS", 1, "PROCEDURE p_archive_orders (p_before IN DATE) AS\n"),
                ("SALES", "P_ARCHIVE_ORDERS", 2, "BEGIN\n  FOR r IN (SELECT order_id FROM sales.orders) LOOP\n"),
                ("SALES", "P_ARCHIVE_ORDERS", 3, "    DELETE FROM sales.orders WHERE order_id = r.order_id;\n"),
                ("SALES", "P_ARCHIVE_ORDERS", 4, "  END LOOP;\nEND;\n"),
                ("HR", "P_OTHER", 1, "PROCEDURE p_other AS BEGIN NULL; END;"),
            ]  # fmt: skip

    class Conn:
        def execute(self, *_a: Any) -> Result:
            return Result()

    procs = _oracle_procedures(Conn(), "FREEPDB1", {"sales"})
    assert [(p.schema_name, p.name) for p in procs] == [("sales", "p_archive_orders")]
    p = procs[0]
    assert p.definition_sql.startswith("CREATE OR REPLACE PROCEDURE p_archive_orders")
    assert "sales.orders" in p.referenced_tables and "sales.orders" in p.target_tables
    assert p.has_cursors


# ---------------------------------------------------------------- Databricks writer
def test_databricks_literals_parse_and_keep_their_values() -> None:
    import duckdb

    values = [
        None,
        True,
        3,
        1.5,
        Decimal("12.50"),
        dt.datetime(2026, 1, 2, 3, 4, 5, 600000),
        dt.date(2026, 1, 2),
        b"\x00\xff",
        "it's a \\ test",
    ]
    sql = "SELECT " + ", ".join(connectors.databricks_literal(v) for v in values)
    tree = sqlglot.parse_one(sql, read="databricks")
    assert duckdb.sql(tree.sql("duckdb")).fetchall()[0] == (
        None,
        True,
        3,
        1.5,
        Decimal("12.50"),
        dt.datetime(2026, 1, 2, 3, 4, 5, 600000),
        dt.date(2026, 1, 2),
        b"\x00\xff",
        "it's a \\ test",
    )
    aware = dt.datetime(2026, 1, 2, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    assert connectors.databricks_literal(aware) == "TIMESTAMP '2026-01-02 04:30:00+00:00'"


def test_databricks_writes_many_rows_per_statement(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    class Cursor:
        def execute(self, sql: str) -> None:
            sent.append(sql)

        def close(self) -> None:
            pass

    class Raw:
        dbapi_connection = type("D", (), {"cursor": lambda self: Cursor()})()

    class Ctx:
        connection = Raw()

        def __enter__(self) -> Ctx:
            return self

        def __exit__(self, *_a: Any) -> None:
            pass

    conn = connectors.DatabricksConnector.__new__(connectors.DatabricksConnector)
    conn.dialect = "databricks"
    conn.engine = type("E", (), {"connect": lambda self: Ctx()})()
    n = 1_234
    table = pa.table({"id": list(range(n)), "note": [f"row {i}" for i in range(n)]})
    assert conn.write_arrow(table, "curated_dbo", "orders") == n
    assert len(sent) == 3  # 500 + 500 + 234 rows, not 1,234 round trips
    assert sent[0].startswith("INSERT INTO curated_dbo.orders (id, note) VALUES")
    for stmt in sent:
        sqlglot.parse_one(stmt, read="databricks")


# ---------------------------------------------------------------- sign-in and drivers
def test_snowflake_key_file_goes_to_the_driver_not_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlalchemy

    seen: dict[str, Any] = {}

    def fake_create_engine(url: Any, **kw: Any) -> str:
        seen["url"], seen["kw"] = url, kw
        return "engine"

    monkeypatch.setattr(sqlalchemy, "create_engine", fake_create_engine)
    uri = CATALOG["snowflake"].build_uri(
        {
            "account": "myorg-acct",
            "username": "L2C_USER",
            "private_key_file": r"C:\Users\me\.l2c\snowflake_rsa_key.p8",
            "database": "L2C_DEMO",
            "warehouse": "COMPUTE_WH",
        }
    )
    assert ":" not in uri.split("@")[0].removeprefix("snowflake://")  # no password in the URL
    connectors.make_engine(uri)
    assert seen["kw"]["connect_args"] == {
        "private_key_file": r"C:\Users\me\.l2c\snowflake_rsa_key.p8",
        "authenticator": "SNOWFLAKE_JWT",
    }
    assert "l2c_private_key_file" not in str(seen["url"])
    assert seen["url"].query["timezone"] == "UTC"
    with pytest.raises(ValueError, match="password or a private key"):
        CATALOG["snowflake"].build_uri({"account": "a", "username": "u", "database": "D"})


def test_sql_server_needs_no_driver_install_by_default() -> None:
    s = {"host": "localhost", "database": "acme", "username": "l2c_reader", "password": "p;w"}
    assert CATALOG["sqlserver"].build_uri(s).startswith("mssql+pymssql://l2c_reader:p%3Bw@")
    odbc = CATALOG["sqlserver"].build_uri(s | {"driver": "ODBC Driver 18 for SQL Server"})
    assert odbc.startswith("mssql+pyodbc://") and "ODBC+Driver+18" in odbc
    fields = {f["name"]: f for f in CATALOG["sqlserver"].spec()["fields"]}
    assert fields["driver"]["default"] == PYMSSQL


# ---------------------------------------------------------------- the demo data
def _seed_module() -> Any:
    spec = importlib.util.spec_from_file_location("seed_demo", ROOT / "demo" / "seed_demo.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_demo_data_and_objects() -> None:
    seed = _seed_module()
    customers, orders = seed.oracle_rows()
    assert (len(customers), len(orders)) == (2_000, 40_000)
    assert any(o[6] == "" for o in orders)  # becomes NULL in Oracle
    assert all(c[0] == i for i, c in enumerate(customers, start=1))
    ms = seed.mssql_rows()
    assert len(ms["dbo.orders"]) == len(ms["finance.gl_entries"]) == 60_000
    assert {o[1] for o in ms["dbo.orders"]} <= {c[0] for c in ms["dbo.customers"]}
    for dialect, objects in (("oracle", seed.ORACLE_OBJECTS), ("tsql", seed.MSSQL_OBJECTS)):
        for ddl in objects:
            if "PROCEDURE" not in ddl:
                sqlglot.parse_one(ddl, read=dialect)
    compose = (ROOT / "demo" / "docker-compose.yml").read_text()
    assert "gvenzl/oracle-free" in compose and "mssql/server:2022" in compose
