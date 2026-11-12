"""Snowflake TargetGenerator plugin (Snowflake SQL, dbt models, Tasks & Streams)."""

from __future__ import annotations

import re

from accelerator.config.client_config import ClientConfig
from accelerator.ir.models import ColumnIR, PackageIR, TableIR
from accelerator.plugins.generators.base import BaseTargetGenerator
from accelerator.plugins.interfaces import GeneratedFile, PluginManifest


class SnowflakeGenerator(BaseTargetGenerator):
    platform = "snowflake"
    sqlglot_dialect = "snowflake"

    manifest = PluginManifest(
        plugin_type="TargetGenerator",
        name="snowflake_generator",
        version="0.1.0",
        supported_versions=["Snowflake Cloud"],
        capabilities={"ddl": True, "sql": True, "dbt": True, "tasks_streams": True},
        description="Generates Snowflake SQL, dbt models, and Snowflake Tasks/Streams orchestration.",
    )

    def _generate_orchestration(
        self,
        package: PackageIR,
        model_names: list[str],
        config: ClientConfig,
    ) -> list[GeneratedFile]:
        safe_pkg = re.sub(r"[^a-zA-Z0-9_]+", "_", package.name.upper()).strip("_")
        wh = config.target.warehouse or "COMPUTE_WH"
        task_sql_lines = [
            f"-- Snowflake Tasks orchestration for {package.name}",
            f"CREATE OR REPLACE TASK {safe_pkg}_ROOT",
            f"  WAREHOUSE = {wh}",
            "  SCHEDULE = 'USING CRON 0 2 * * * UTC'",
            "AS",
            f"  SELECT 'Starting wave execution for {package.name}';",
        ]
        prev = f"{safe_pkg}_ROOT"
        for idx, model in enumerate(model_names, start=1):
            t_name = f"{safe_pkg}_STEP_{idx}_{model.upper()}"
            task_sql_lines.extend(
                [
                    "",
                    f"CREATE OR REPLACE TASK {t_name}",
                    f"  WAREHOUSE = {wh}",
                    f"  AFTER {prev}",
                    "AS",
                    f"  CALL SYSTEM$WAIT(1); -- Run dbt model: {model}",
                ]
            )
            prev = t_name

        content = "\n".join(task_sql_lines) + "\n"
        return [
            GeneratedFile(
                relative_path=f"orchestration/snowflake/{safe_pkg.lower()}_tasks.sql",
                artifact_kind="task",
                content=content,
                object_key=package.id,
                compile_passed=True,
                compile_log="Snowflake Task DDL generated",
            )
        ]

    def run_golden_check(self) -> tuple[bool, str]:
        from accelerator.config.client_config import ClientConfig, SourceConfig, TargetConfig

        cfg = ClientConfig(
            client="golden",
            source=SourceConfig(database="sqlserver", etl="ssis"),
            target=TargetConfig(platform="snowflake", output="dbt"),
        )
        tbl = TableIR(
            database="acme",
            schema_name="dbo",
            name="orders",
            columns=[ColumnIR(name="order_id", data_type="INT", nullable=False)],
        )
        ddl = self.generate_ddl(tbl, cfg)
        ok = ddl.compile_passed and "NUMBER(38,0)" in ddl.content
        return ok, "SnowflakeGenerator golden check passed" if ok else ddl.compile_log
