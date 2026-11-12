"""Oracle SourceCollector plugin (ALL_* dictionary views + offline PL/SQL DDL support)."""

from __future__ import annotations

from accelerator.ir.models import CatalogIR, ColumnIR, TableIR
from accelerator.plugins.collectors.base_sql import BaseSqlCollector, parse_ddl_to_catalog
from accelerator.plugins.interfaces import PluginManifest

ORACLE_CATALOG_SQL = """
SELECT
    c.OWNER AS schema_name,
    c.TABLE_NAME AS table_name,
    c.COLUMN_NAME AS column_name,
    c.DATA_TYPE AS data_type,
    c.DATA_LENGTH AS data_length,
    c.DATA_PRECISION AS data_precision,
    c.DATA_SCALE AS data_scale,
    c.NULLABLE AS nullable,
    c.COLUMN_ID AS ordinal_position,
    t.NUM_ROWS AS num_rows
FROM ALL_TAB_COLUMNS c
JOIN ALL_TABLES t ON c.OWNER = t.OWNER AND c.TABLE_NAME = t.TABLE_NAME
WHERE c.OWNER NOT IN ('SYS', 'SYSTEM', 'OUTLN', 'XDB')
ORDER BY c.OWNER, c.TABLE_NAME, c.COLUMN_ID
"""


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

    def _collect_live_enterprise(
        self, resolved_uri: str, schemas: list[str] | None = None
    ) -> CatalogIR:
        import oracledb

        conn = oracledb.connect(dsn=resolved_uri)
        try:
            cur = conn.cursor()
            rows = cur.execute(ORACLE_CATALOG_SQL).fetchall()
            grouped: dict[tuple[str, str], TableIR] = {}
            allowed = {s.lower() for s in schemas} if schemas else None
            for (
                schema_name,
                table_name,
                col_name,
                dtype,
                length,
                prec,
                scale,
                nullable,
                ord_pos,
                num_rows,
            ) in rows:
                if allowed and str(schema_name).lower() not in allowed:
                    continue
                key = (str(schema_name), str(table_name))
                if key not in grouped:
                    grouped[key] = TableIR(
                        database="ORCL",
                        schema_name=key[0],
                        name=key[1],
                        row_count=int(num_rows or 0),
                    )
                grouped[key].columns.append(
                    ColumnIR(
                        name=str(col_name),
                        data_type=str(dtype),
                        length=int(length) if length is not None else None,
                        precision=int(prec) if prec is not None else None,
                        scale=int(scale) if scale is not None else None,
                        nullable=(str(nullable).upper() == "Y"),
                        ordinal_position=int(ord_pos),
                    )
                )
            return CatalogIR(
                source_system=self.source_system,
                database_name="ORCL",
                tables=list(grouped.values()),
            )
        finally:
            conn.close()

    def run_golden_check(self) -> tuple[bool, str]:
        sample = (
            "CREATE TABLE FINANCE.LEDGER (LEDGER_ID NUMBER(10) PRIMARY KEY, AMOUNT NUMBER(18,2));"
        )
        cat = parse_ddl_to_catalog(sample, "oracle", "oracle", "ORCL")
        ok = len(cat.tables) == 1 and cat.tables[0].name == "LEDGER"
        return ok, "OracleCollector golden DDL check passed" if ok else "Failed golden check"
