"""Connector interface and implementations for DuckDB, generic SQLAlchemy, Snowflake, Databricks, Fabric."""

from __future__ import annotations

import io
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import sqlglot
from sqlglot import exp

from accelerator.core.secrets import resolve_connection_uri

# How many rows go into one INSERT round trip on engines without a native bulk path.
INSERT_CHUNK_ROWS = 1000

_SIMPLE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED = {
    "all",
    "and",
    "as",
    "by",
    "case",
    "check",
    "column",
    "constraint",
    "create",
    "cross",
    "date",
    "default",
    "delete",
    "desc",
    "distinct",
    "drop",
    "else",
    "end",
    "from",
    "full",
    "grant",
    "group",
    "having",
    "in",
    "inner",
    "insert",
    "into",
    "is",
    "join",
    "key",
    "left",
    "like",
    "limit",
    "not",
    "null",
    "on",
    "or",
    "order",
    "outer",
    "primary",
    "references",
    "right",
    "select",
    "set",
    "table",
    "then",
    "time",
    "timestamp",
    "to",
    "union",
    "unique",
    "update",
    "user",
    "using",
    "values",
    "when",
    "where",
    "with",
}


class Connector(ABC):
    """A live connection to one database engine."""

    kind: str = "generic"
    #: sqlglot dialect used to render SQL for this engine
    dialect: str = "postgres"

    # -- identifiers ---------------------------------------------------------------------------
    def quote(self, ident: str) -> str:
        """Leave plain names unquoted so every engine applies its own case rule consistently
        (Snowflake upper-cases, Postgres lower-cases) - the same way generated DDL and dbt models
        refer to them. Only names that need it (spaces, reserved words) are quoted."""
        if _SIMPLE_IDENT.match(ident) and ident.lower() not in _RESERVED:
            return ident
        return exp.to_identifier(ident, quoted=True).sql(dialect=self.dialect)

    def qualified(self, schema: str, name: str) -> str:
        return f"{self.quote(schema)}.{self.quote(name)}" if schema else self.quote(name)

    def render(self, expression: exp.Expression) -> str:
        return expression.sql(dialect=self.dialect)

    def transpile(self, sql: str, read: str) -> list[str]:
        """Translate SQL written for another dialect (e.g. generated Snowflake DDL) to this engine."""
        if read == self.dialect:
            return [s.strip() for s in sql.split(";") if s.strip()]
        return [s for s in sqlglot.transpile(sql, read=read, write=self.dialect) if s.strip()]

    # -- execution -----------------------------------------------------------------------------
    @abstractmethod
    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None: ...

    @abstractmethod
    def query(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]: ...

    @abstractmethod
    def fetch_arrow(self, sql: str, params: Sequence[Any] | None = None) -> pa.Table:
        """Run a query and return its full result as an Arrow table (callers page with LIMIT)."""

    @abstractmethod
    def write_arrow(self, data: pa.Table, schema: str, name: str) -> int:
        """Insert rows into an existing table, matching columns by name, in one transaction."""

    def table_exists(self, schema: str, name: str) -> bool:
        try:
            self.query(f"SELECT 1 FROM {self.qualified(schema, name)} WHERE 1 = 0")
            return True
        except Exception:
            return False

    def execute_in_transaction(self, statements: Sequence[str]) -> None:
        """Run several statements atomically (used for incremental merges)."""
        self.execute("BEGIN")
        try:
            for st in statements:
                self.execute(st)
            self.execute("COMMIT")
        except Exception:
            self.execute("ROLLBACK")
            raise

    def create_schema(self, schema: str) -> None:
        if schema:
            self.execute(f"CREATE SCHEMA IF NOT EXISTS {self.quote(schema)}")

    def truncate(self, schema: str, name: str) -> None:
        self.execute(f"DELETE FROM {self.qualified(schema, name)}")

    def drop_table(self, schema: str, name: str) -> None:
        self.execute(f"DROP TABLE IF EXISTS {self.qualified(schema, name)}")

    def create_empty_like(self, schema: str, name: str, like_schema: str, like_name: str) -> None:
        src = self.qualified(like_schema, like_name)
        self.execute(
            f"CREATE TABLE {self.qualified(schema, name)} AS SELECT * FROM {src} WHERE 1 = 0"
        )

    def close(self) -> None:  # noqa: B027 - optional hook
        pass

    def __enter__(self) -> Connector:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# ----------------------------------------------------------------------------------------------
class DuckDBConnector(Connector):
    kind = "duckdb"
    dialect = "duckdb"

    def transpile(self, sql: str, read: str) -> list[str]:
        # DuckDB text and binary types have no length limit: VARCHAR(MAX) is just VARCHAR.
        return [
            re.sub(r"\(\s*MAX\s*\)", "", s, flags=re.IGNORECASE)
            for s in super().transpile(sql, read)
        ]

    def __init__(self, path: str, read_only: bool = False) -> None:
        import duckdb

        self.conn = duckdb.connect(path, read_only=read_only)

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        self.conn.execute(sql, list(params) if params else None)

    def query(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
        return self.conn.execute(sql, list(params) if params else None).fetchall()

    def fetch_arrow(self, sql: str, params: Sequence[Any] | None = None) -> pa.Table:
        rel = self.conn.sql(sql, params=list(params) if params else None)
        if hasattr(rel, "to_arrow_table"):  # DuckDB >= 1.4 / 2.x, no extra copy
            return rel.to_arrow_table()
        return rel.arrow()  # older DuckDB

    def write_arrow(self, data: pa.Table, schema: str, name: str) -> int:
        self.conn.register("_l2c_batch", data)
        try:
            self.conn.execute("BEGIN")
            self.conn.execute(
                f"INSERT INTO {self.qualified(schema, name)} BY NAME SELECT * FROM _l2c_batch"
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            self.conn.unregister("_l2c_batch")
        return data.num_rows

    def close(self) -> None:
        self.conn.close()


# ----------------------------------------------------------------------------------------------
_SQLA_DIALECTS = {
    "postgresql": "postgres",
    "mssql": "tsql",
    "sqlite": "sqlite",
    "mysql": "mysql",
    "oracle": "oracle",
    "teradatasql": "teradata",
    "teradata": "teradata",
    "snowflake": "snowflake",
    "databricks": "databricks",
}


class SqlAlchemyConnector(Connector):
    """Any engine with a SQLAlchemy dialect. Rows are written with batched INSERTs."""

    kind = "sqlalchemy"

    def __init__(self, url: str) -> None:
        from sqlalchemy import create_engine

        self.engine = create_engine(url, future=True, pool_pre_ping=True)
        self.dialect = _SQLA_DIALECTS.get(self.engine.dialect.name, "postgres")
        self.kind = {"postgres": "postgres", "tsql": "sqlserver"}.get(self.dialect, self.dialect)

    # SQLAlchemy text() uses :name parameters; we accept positional and number them.
    @staticmethod
    def _bind(sql: str, params: Sequence[Any] | None) -> tuple[str, dict[str, Any]]:
        if not params:
            return sql, {}
        parts = sql.split("?")
        if len(parts) - 1 != len(params):
            raise ValueError("Parameter count does not match placeholders")
        out = parts[0]
        for i, p in enumerate(parts[1:]):
            out += f":p{i}{p}"
        return out, {f"p{i}": v for i, v in enumerate(params)}

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        from sqlalchemy import text

        stmt, bind = self._bind(sql, params)
        with self.engine.begin() as conn:
            conn.execute(text(stmt), bind)

    def query(self, sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
        from sqlalchemy import text

        stmt, bind = self._bind(sql, params)
        with self.engine.connect() as conn:
            return [tuple(r) for r in conn.execute(text(stmt), bind)]

    def fetch_arrow(self, sql: str, params: Sequence[Any] | None = None) -> pa.Table:
        from sqlalchemy import text

        stmt, bind = self._bind(sql, params)
        with self.engine.connect() as conn:
            result = conn.execute(text(stmt), bind)
            cols = list(result.keys())
            rows = result.fetchall()
        return (
            pa.Table.from_pylist([dict(zip(cols, r, strict=True)) for r in rows])
            if rows
            else pa.table({c: pa.array([], type=pa.null()) for c in cols})
        )

    def write_arrow(self, data: pa.Table, schema: str, name: str) -> int:
        from sqlalchemy import text

        if data.num_rows == 0:
            return 0
        cols = data.column_names
        stmt = text(
            f"INSERT INTO {self.qualified(schema, name)} ({', '.join(self.quote(c) for c in cols)}) "
            f"VALUES ({', '.join(f':c{i}' for i in range(len(cols)))})"
        )
        rows = data.to_pylist()
        with self.engine.begin() as conn:
            for start in range(0, len(rows), INSERT_CHUNK_ROWS):
                chunk = rows[start : start + INSERT_CHUNK_ROWS]
                conn.execute(stmt, [{f"c{i}": r[c] for i, c in enumerate(cols)} for r in chunk])
        return data.num_rows

    def execute_in_transaction(self, statements: Sequence[str]) -> None:
        from sqlalchemy import text

        with self.engine.begin() as conn:
            for st in statements:
                conn.execute(text(st))

    def create_schema(self, schema: str) -> None:
        if not schema:
            return
        if self.dialect == "tsql":  # SQL Server / Fabric have no CREATE SCHEMA IF NOT EXISTS
            self.execute(
                f"IF SCHEMA_ID(N'{schema}') IS NULL EXEC('CREATE SCHEMA {self.quote(schema)}')"
            )
        else:
            super().create_schema(schema)

    def create_empty_like(self, schema: str, name: str, like_schema: str, like_name: str) -> None:
        if self.dialect == "tsql":
            self.execute(
                f"SELECT * INTO {self.qualified(schema, name)} FROM {self.qualified(like_schema, like_name)} WHERE 1 = 0"
            )
        else:
            super().create_empty_like(schema, name, like_schema, like_name)

    def drop_table(self, schema: str, name: str) -> None:
        if self.dialect == "tsql":
            self.execute(f"DROP TABLE IF EXISTS {self.qualified(schema, name)}")
        else:
            super().drop_table(schema, name)

    def close(self) -> None:
        self.engine.dispose()


class SnowflakeConnector(SqlAlchemyConnector):
    """Snowflake: writes through `write_pandas`, which stages Parquet and runs COPY INTO."""

    def write_arrow(self, data: pa.Table, schema: str, name: str) -> int:
        if data.num_rows == 0:
            return 0
        from snowflake.connector.pandas_tools import write_pandas

        with self.engine.connect() as conn:
            raw = conn.connection.dbapi_connection
            # Unquoted (upper-cased) names, matching how the generated DDL created the table.
            df = data.to_pandas()
            df.columns = [c.upper() for c in df.columns]
            ok, _chunks, nrows, _ = write_pandas(
                raw, df, table_name=name.upper(), schema=schema.upper(), quote_identifiers=False
            )
        if not ok:
            raise RuntimeError(f"Snowflake write_pandas failed for {schema}.{name}")
        return int(nrows)


class DatabricksConnector(SqlAlchemyConnector):
    """Databricks SQL warehouse. Bulk loads from cloud staging use COPY INTO; otherwise INSERTs."""


class FabricConnector(SqlAlchemyConnector):
    """Microsoft Fabric Warehouse via its SQL endpoint (T-SQL). Bulk loads use COPY INTO from
    OneLake/ADLS staging when the artifact store is ADLS; otherwise batched INSERTs."""

    kind = "fabric"


def open_connector(reference: str, read_only: bool = False) -> Connector:
    """Resolve a vault/env reference (or a direct URI) and open the matching connector."""
    uri = resolve_connection_uri(reference)
    if uri.startswith("duckdb:///"):
        return DuckDBConnector(uri.removeprefix("duckdb:///"), read_only=read_only)
    if uri.startswith("snowflake://"):
        return SnowflakeConnector(uri)
    if uri.startswith("databricks://"):
        return DatabricksConnector(uri)
    if uri.startswith("mssql") and "fabric.microsoft.com" in uri.lower():
        return FabricConnector(uri)
    return SqlAlchemyConnector(uri)


def arrow_to_parquet_bytes(data: pa.Table) -> bytes:
    buf = io.BytesIO()
    pq.write_table(data, buf)
    return buf.getvalue()


def iter_batches(table: pa.Table, rows: int) -> Iterator[pa.Table]:
    for start in range(0, table.num_rows, rows):
        yield table.slice(start, rows)
