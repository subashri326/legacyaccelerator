"""Databricks TargetGenerator plugin (Spark SQL / PySpark, dbt models, Lakeflow Jobs)."""

from __future__ import annotations

import json
import re

from accelerator.config.client_config import ClientConfig
from accelerator.ir.models import ColumnIR, PackageIR, TableIR
from accelerator.plugins.generators.base import BaseTargetGenerator
from accelerator.plugins.interfaces import GeneratedBundle, GeneratedFile, PluginManifest


class DatabricksGenerator(BaseTargetGenerator):
    platform = "databricks"
    sqlglot_dialect = "databricks"

    manifest = PluginManifest(
        plugin_type="TargetGenerator",
        name="databricks_generator",
        version="0.1.0",
        supported_versions=["Databricks Runtime 14+", "Unity Catalog"],
        capabilities={"ddl": True, "spark_sql": True, "pyspark": True, "dbt": True, "lakeflow_jobs": True},
        description="Generates Databricks SQL/PySpark, dbt models, and Lakeflow Jobs definitions.",
    )

    def generate_package(
        self,
        package: PackageIR,
        tables: dict[str, TableIR],
        config: ClientConfig,
        accepted_overrides: dict[str, str] | None = None,
    ) -> GeneratedBundle:
        bundle = super().generate_package(package, tables, config, accepted_overrides)
        if config.target.output == "native":
            # Also emit PySpark job wrapper for native Databricks mode
            for df in package.data_flows:
                safe_name = re.sub(r"[^a-zA-Z0-9_]+", "_", df.name.lower()).strip("_")
                srcs = [s.split(".")[-1] for s in df.source_tables()]
                tgts = [t.split(".")[-1] for t in df.target_tables()]
                pyspark_code = (
                    f"# Databricks PySpark Notebook generated for {package.name} :: {df.name}\n"
                    "from pyspark.sql import SparkSession\n\n"
                    "spark = SparkSession.builder.getOrCreate()\n"
                    + "\n".join(f"df_{i} = spark.table('{s}')" for i, s in enumerate(srcs, 1))
                    + "\n# Apply SQL transformation logic via Spark SQL\n"
                    + f"# Target table(s): {tgts}\n"
                )
                bundle.files.append(
                    GeneratedFile(
                        relative_path=f"pyspark/{safe_name}.py",
                        artifact_kind="pyspark",
                        content=pyspark_code,
                        object_key=package.id,
                    )
                )
        return bundle

    def _generate_orchestration(
        self,
        package: PackageIR,
        model_names: list[str],
        config: ClientConfig,
    ) -> list[GeneratedFile]:
        safe_pkg = re.sub(r"[^a-zA-Z0-9_]+", "_", package.name.lower()).strip("_")
        tasks = []
        prev_key: str | None = None
        for model in model_names:
            t_key = f"run_{model}"
            task_def: dict[str, object] = {
                "task_key": t_key,
                "spark_python_task": {"python_file": f"pyspark/{model}.py"},
            }
            if prev_key:
                task_def["depends_on"] = [{"task_key": prev_key}]
            tasks.append(task_def)
            prev_key = t_key

        job_json = json.dumps(
            {
                "name": f"l2c_{safe_pkg}",
                "format": "MULTI_TASK",
                "tasks": tasks,
                "tags": {"accelerator": "l2c", "source_tool": package.source_tool},
            },
            indent=2,
        )
        return [
            GeneratedFile(
                relative_path=f"orchestration/databricks/{safe_pkg}_lakeflow_job.json",
                artifact_kind="pipeline_json",
                content=job_json,
                object_key=package.id,
            )
        ]

    def run_golden_check(self) -> tuple[bool, str]:
        from accelerator.config.client_config import ClientConfig, SourceConfig, TargetConfig

        cfg = ClientConfig(
            client="golden",
            source=SourceConfig(database="sqlserver", etl="ssis"),
            target=TargetConfig(platform="databricks", output="dbt"),
        )
        tbl = TableIR(
            database="acme",
            schema_name="dbo",
            name="orders",
            columns=[ColumnIR(name="cust_name", data_type="NVARCHAR(100)", nullable=True)],
        )
        ddl = self.generate_ddl(tbl, cfg)
        ok = ddl.compile_passed and "STRING" in ddl.content
        return ok, "DatabricksGenerator golden check passed" if ok else ddl.compile_log
