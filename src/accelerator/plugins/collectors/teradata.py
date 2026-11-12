"""Teradata SourceCollector plugin (DBC.* views + offline BTEQ/DDL support)."""

from __future__ import annotations

from accelerator.ir.models import CatalogIR
from accelerator.plugins.collectors.base_sql import BaseSqlCollector, parse_ddl_to_catalog
from accelerator.plugins.interfaces import PluginManifest

TERADATA_CATALOG_SQL = """
SELECT
    c.DatabaseName AS schema_name,
    c.TableName AS table_name,
    c.ColumnName AS column_name,
    c.ColumnType AS data_type,
    c.ColumnLength AS data_length,
    c.DecimalTotalDigits AS data_precision,
    c.DecimalFractionalDigits AS data_scale,
    c.Nullable AS nullable,
    c.ColumnId AS ordinal_position
FROM DBC.ColumnsV c
JOIN DBC.TablesV t ON c.DatabaseName = t.DatabaseName AND c.TableName = t.TableName
WHERE t.TableKind IN ('T', 'O')
ORDER BY c.DatabaseName, c.TableName, c.ColumnId;
"""


class TeradataCollector(BaseSqlCollector):
    dialect = "teradata"
    source_system = "teradata"

    manifest = PluginManifest(
        plugin_type="SourceCollector",
        name="teradata_collector",
        version="0.1.0",
        supported_versions=["Teradata 16+", "Teradata Vantage"],
        capabilities={"catalog": True, "views": True, "bteq": True, "offline_ddl": True},
        description="Read-only harvester for Teradata DBC.* system views and offline BTEQ/DDL exports.",
    )

    def _collect_live_enterprise(
        self, resolved_uri: str, schemas: list[str] | None = None
    ) -> CatalogIR:
        raise NotImplementedError(
            "Live Teradata ODBC requires teradatasql driver or ODBC DSN; pass catalog_bundle_path for offline DDL/JSON."
        )

    def run_golden_check(self) -> tuple[bool, str]:
        sample = "CREATE TABLE EDW.CUST_DIM (CUST_ID INTEGER NOT NULL, CUST_NM VARCHAR(100));"
        cat = parse_ddl_to_catalog(sample, "teradata", "teradata", "TERADATA_EDW")
        ok = len(cat.tables) == 1 and cat.tables[0].name == "CUST_DIM"
        return ok, "TeradataCollector golden DDL check passed" if ok else "Failed golden check"
