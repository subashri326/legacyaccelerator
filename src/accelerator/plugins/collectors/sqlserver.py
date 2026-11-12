"""SQL Server SourceCollector plugin (sys.* catalog queries + offline DDL/DuckDB support)."""

from __future__ import annotations

from accelerator.ir.models import CatalogIR, ColumnIR, TableIR
from accelerator.plugins.collectors.base_sql import BaseSqlCollector, parse_ddl_to_catalog
from accelerator.plugins.interfaces import PluginManifest

SQLSERVER_CATALOG_SQL = """
SELECT
    DB_NAME() AS database_name,
    s.name AS schema_name,
    t.name AS table_name,
    c.name AS column_name,
    ty.name AS data_type,
    c.max_length,
    c.precision,
    c.scale,
    c.is_nullable,
    ISNULL(i.is_primary_key, 0) AS is_primary_key,
    c.column_id AS ordinal_position,
    SUM(p.rows) AS row_count
FROM sys.tables t
JOIN sys.schemas s ON t.schema_id = s.schema_id
JOIN sys.columns c ON t.object_id = c.object_id
JOIN sys.types ty ON c.user_type_id = ty.user_type_id
LEFT JOIN sys.partitions p ON t.object_id = p.object_id AND p.index_id IN (0, 1)
LEFT JOIN sys.index_columns ic ON c.object_id = ic.object_id AND c.column_id = ic.column_id
LEFT JOIN sys.indexes i ON ic.object_id = i.object_id AND ic.index_id = i.index_id AND i.is_primary_key = 1
GROUP BY s.name, t.name, c.name, ty.name, c.max_length, c.precision, c.scale, c.is_nullable, i.is_primary_key, c.column_id
ORDER BY s.name, t.name, c.column_id;
"""


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

    def _collect_live_enterprise(
        self, resolved_uri: str, schemas: list[str] | None = None
    ) -> CatalogIR:
        import pyodbc

        conn = pyodbc.connect(resolved_uri, readonly=True, timeout=15)
        try:
            cur = conn.cursor()
            rows = cur.execute(SQLSERVER_CATALOG_SQL).fetchall()
            grouped: dict[tuple[str, str], TableIR] = {}
            db_name = "sqlserver_db"
            allowed = {s.lower() for s in schemas} if schemas else None
            for r in rows:
                db_name = str(r.database_name or db_name)
                if allowed and str(r.schema_name).lower() not in allowed:
                    continue
                key = (str(r.schema_name), str(r.table_name))
                if key not in grouped:
                    grouped[key] = TableIR(
                        database=db_name,
                        schema_name=key[0],
                        name=key[1],
                        row_count=int(r.row_count or 0),
                    )
                grouped[key].columns.append(
                    ColumnIR(
                        name=str(r.column_name),
                        data_type=str(r.data_type),
                        length=int(r.max_length) if r.max_length is not None else None,
                        precision=int(r.precision) if r.precision is not None else None,
                        scale=int(r.scale) if r.scale is not None else None,
                        nullable=bool(r.is_nullable),
                        is_primary_key=bool(r.is_primary_key),
                        ordinal_position=int(r.ordinal_position),
                    )
                )
            return CatalogIR(
                source_system=self.source_system,
                database_name=db_name,
                tables=list(grouped.values()),
            )
        finally:
            conn.close()

    def run_golden_check(self) -> tuple[bool, str]:
        sample = "CREATE TABLE dbo.orders (order_id INT NOT NULL PRIMARY KEY, total DECIMAL(12,2));"
        cat = parse_ddl_to_catalog(sample, "sqlserver", "tsql", "acme")
        ok = len(cat.tables) == 1 and cat.tables[0].name == "orders" and len(cat.tables[0].columns) == 2
        return ok, "SqlServerCollector golden DDL check passed" if ok else "Failed golden check"
