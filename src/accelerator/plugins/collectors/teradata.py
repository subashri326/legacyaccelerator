"""Teradata SourceCollector plugin (DBC.* views + offline BTEQ/DDL support)."""

from __future__ import annotations

from accelerator.plugins.collectors.base_sql import BaseSqlCollector, parse_ddl_to_catalog
from accelerator.plugins.interfaces import PluginManifest


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

    def run_golden_check(self) -> tuple[bool, str]:
        sample = "CREATE TABLE EDW.CUST_DIM (CUST_ID INTEGER NOT NULL, CUST_NM VARCHAR(100));"
        cat = parse_ddl_to_catalog(sample, "teradata", "teradata", "TERADATA_EDW")
        ok = len(cat.tables) == 1 and cat.tables[0].name == "CUST_DIM"
        return ok, "TeradataCollector golden DDL check passed" if ok else "Failed golden check"
