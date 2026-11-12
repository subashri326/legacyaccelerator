"""SQL Server SourceCollector plugin (sys.* catalog queries + offline DDL/DuckDB support)."""

from __future__ import annotations

from accelerator.plugins.collectors.base_sql import BaseSqlCollector, parse_ddl_to_catalog
from accelerator.plugins.interfaces import PluginManifest


class SqlServerCollector(BaseSqlCollector):
    dialect = "tsql"
    source_system = "sqlserver"

    manifest = PluginManifest(
        plugin_type="SourceCollector",
        name="sqlserver_collector",
        version="0.1.0",
        supported_versions=["SQL Server 2016+", "Azure SQL", "DuckDB/SQLite sandbox"],
        capabilities={"catalog": True, "views": True, "procedures": True, "offline_ddl": True},
        description="Read-only harvester for SQL Server sys.* views and offline T-SQL DDL bundles.",
    )

    def run_golden_check(self) -> tuple[bool, str]:
        sample = "CREATE TABLE dbo.orders (order_id INT NOT NULL PRIMARY KEY, total DECIMAL(12,2));"
        cat = parse_ddl_to_catalog(sample, "sqlserver", "tsql", "acme")
        ok = (
            len(cat.tables) == 1
            and cat.tables[0].name == "orders"
            and len(cat.tables[0].columns) == 2
        )
        return ok, "SqlServerCollector golden DDL check passed" if ok else "Failed golden check"


class PostgresCollector(BaseSqlCollector):
    dialect = "postgres"
    source_system = "postgres"

    manifest = PluginManifest(
        plugin_type="SourceCollector",
        name="postgres_collector",
        version="0.1.0",
        supported_versions=["PostgreSQL 12+"],
        capabilities={"catalog": True, "views": True, "procedures": True, "foreign_keys": True},
        description="Read-only harvester for PostgreSQL via the SQLAlchemy inspector.",
    )

    def run_golden_check(self) -> tuple[bool, str]:
        cat = parse_ddl_to_catalog(
            "CREATE TABLE s.t (id INTEGER PRIMARY KEY);", "postgres", "postgres", "db"
        )
        ok = len(cat.tables) == 1 and cat.tables[0].columns[0].is_primary_key
        return ok, "PostgresCollector golden check passed" if ok else "Failed golden check"
