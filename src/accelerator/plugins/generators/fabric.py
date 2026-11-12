"""Microsoft Fabric TargetGenerator plugin (Fabric Warehouse T-SQL, dbt, Data Factory JSON)."""

from __future__ import annotations

import json
import re

from accelerator.config.client_config import ClientConfig
from accelerator.ir.models import ColumnIR, PackageIR, TableIR
from accelerator.plugins.generators.base import BaseTargetGenerator
from accelerator.plugins.interfaces import GeneratedFile, PluginManifest


class FabricGenerator(BaseTargetGenerator):
    platform = "fabric"
    sqlglot_dialect = "tsql"

    manifest = PluginManifest(
        plugin_type="TargetGenerator",
        name="fabric_generator",
        version="0.1.0",
        supported_versions=["Microsoft Fabric Warehouse", "Fabric Lakehouse"],
        capabilities={"ddl": True, "tsql": True, "dbt": True, "data_factory_pipeline": True},
        description="Generates Microsoft Fabric Warehouse T-SQL, dbt models, and Data Factory pipeline JSON.",
    )

    def _generate_orchestration(
        self,
        package: PackageIR,
        model_names: list[str],
        config: ClientConfig,
    ) -> list[GeneratedFile]:
        safe_pkg = re.sub(r"[^a-zA-Z0-9_]+", "_", package.name.lower()).strip("_")
        activities = []
        prev_name: str | None = None
        for model in model_names:
            act: dict[str, object] = {
                "name": f"Exec_{model}",
                "type": "SqlServerStoredProcedure",
                "typeProperties": {"storedProcedureName": f"sp_run_{model}"},
            }
            if prev_name:
                act["dependsOn"] = [{"activity": prev_name, "dependencyConditions": ["Succeeded"]}]
            activities.append(act)
            prev_name = f"Exec_{model}"

        adf_json = json.dumps(
            {
                "name": f"pl_{safe_pkg}",
                "properties": {
                    "activities": activities,
                    "annotations": ["l2c-accelerator", package.source_tool],
                },
            },
            indent=2,
        )
        return [
            GeneratedFile(
                relative_path=f"orchestration/fabric/{safe_pkg}_adf_pipeline.json",
                artifact_kind="pipeline_json",
                content=adf_json,
                object_key=package.id,
            )
        ]

    def run_golden_check(self) -> tuple[bool, str]:
        from accelerator.config.client_config import ClientConfig, SourceConfig, TargetConfig

        cfg = ClientConfig(
            client="golden",
            source=SourceConfig(database="sqlserver", etl="ssis"),
            target=TargetConfig(platform="fabric", output="dbt"),
        )
        tbl = TableIR(
            database="acme",
            schema_name="dbo",
            name="orders",
            columns=[ColumnIR(name="created_at", data_type="DATETIME2", nullable=False)],
        )
        ddl = self.generate_ddl(tbl, cfg)
        ok = ddl.compile_passed and "DATETIME2(6)" in ddl.content
        return ok, "FabricGenerator golden check passed" if ok else ddl.compile_log
