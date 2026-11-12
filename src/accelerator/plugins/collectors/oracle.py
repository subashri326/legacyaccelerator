"""Oracle SourceCollector plugin (ALL_* dictionary views + offline PL/SQL DDL support)."""

from __future__ import annotations

from accelerator.plugins.collectors.base_sql import BaseSqlCollector, parse_ddl_to_catalog
from accelerator.plugins.interfaces import PluginManifest


class OracleCollector(BaseSqlCollector):
    dialect = "oracle"
    source_system = "oracle"

    manifest = PluginManifest(
        plugin_type="SourceCollector",
        name="oracle_collector",
        version="0.1.0",
        supported_versions=["Oracle 12c+", "Oracle 19c", "Oracle 21c"],
        capabilities={"catalog": True, "views": True, "procedures": True, "offline_ddl": True},
        description="Read-only harvester for Oracle ALL_* dictionary views and offline PL/SQL DDL.",
    )

    def run_golden_check(self) -> tuple[bool, str]:
        sample = (
            "CREATE TABLE FINANCE.LEDGER (LEDGER_ID NUMBER(10) PRIMARY KEY, AMOUNT NUMBER(18,2));"
        )
        cat = parse_ddl_to_catalog(sample, "oracle", "oracle", "ORCL")
        ok = len(cat.tables) == 1 and cat.tables[0].name == "LEDGER"
        return ok, "OracleCollector golden DDL check passed" if ok else "Failed golden check"
