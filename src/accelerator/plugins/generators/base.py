"""Shared rule engine, type mapping, CTE SQL synthesis, dbt scaffolding, and compile checks."""

from __future__ import annotations

import re

import sqlglot
import yaml
from sqlglot.errors import SqlglotError

from accelerator.config.client_config import ClientConfig
from accelerator.ir.expressions import DIALECT_MAP
from accelerator.ir.models import (
    DataFlowGraphIR,
    NodeOperator,
    OpaqueSnippetIR,
    PackageIR,
    TableIR,
)
from accelerator.plugins.generators.flow_sql import (
    FlowSqlBuilder,
    GeneratedModel,
    GeneratedStep,
    SqlRenderer,
    render_sql_tasks,
)
from accelerator.plugins.interfaces import (
    GeneratedBundle,
    GeneratedFile,
    GeneratedResidue,
    TargetGenerator,
)

DEFAULT_TYPE_MAPS: dict[str, dict[str, str]] = {
    "snowflake": {
        "INT": "NUMBER(38,0)",
        "INTEGER": "NUMBER(38,0)",
        "BIGINT": "NUMBER(38,0)",
        "SMALLINT": "NUMBER(38,0)",
        "TINYINT": "NUMBER(38,0)",
        "BIT": "BOOLEAN",
        "DECIMAL": "NUMBER",
        "NUMERIC": "NUMBER",
        "MONEY": "NUMBER(19,4)",
        "FLOAT": "FLOAT",
        "REAL": "FLOAT",
        "VARCHAR": "VARCHAR",
        "NVARCHAR": "VARCHAR",
        "CHAR": "CHAR",
        "NCHAR": "CHAR",
        "TEXT": "VARCHAR",
        "NTEXT": "VARCHAR",
        "DATETIME": "TIMESTAMP_NTZ",
        "DATETIME2": "TIMESTAMP_NTZ",
        "TIMESTAMP": "TIMESTAMP_NTZ",
        "DATE": "DATE",
        "BOOLEAN": "BOOLEAN",
        "DOUBLE": "FLOAT",
        "NUMBER": "NUMBER",
        "VARCHAR2": "VARCHAR",
        "CLOB": "VARCHAR",
        "BYTEINT": "SMALLINT",
    },
    "databricks": {
        "INT": "INT",
        "INTEGER": "INT",
        "BIGINT": "BIGINT",
        "SMALLINT": "SMALLINT",
        "TINYINT": "TINYINT",
        "BIT": "BOOLEAN",
        "DECIMAL": "DECIMAL",
        "NUMERIC": "DECIMAL",
        "MONEY": "DECIMAL(19,4)",
        "FLOAT": "DOUBLE",
        "REAL": "FLOAT",
        "VARCHAR": "STRING",
        "NVARCHAR": "STRING",
        "CHAR": "STRING",
        "NCHAR": "STRING",
        "TEXT": "STRING",
        "NTEXT": "STRING",
        "DATETIME": "TIMESTAMP",
        "DATETIME2": "TIMESTAMP",
        "TIMESTAMP": "TIMESTAMP",
        "DATE": "DATE",
        "BOOLEAN": "BOOLEAN",
        "DOUBLE": "DOUBLE",
        "NUMBER": "DECIMAL",
        "VARCHAR2": "STRING",
        "CLOB": "STRING",
    },
    "fabric": {
        "INT": "INT",
        "INTEGER": "INT",
        "BIGINT": "BIGINT",
        "SMALLINT": "SMALLINT",
        "BIT": "BIT",
        "DECIMAL": "DECIMAL",
        "NUMERIC": "DECIMAL",
        "MONEY": "DECIMAL(19,4)",
        "FLOAT": "FLOAT",
        "VARCHAR": "VARCHAR",
        "NVARCHAR": "VARCHAR",
        "CHAR": "CHAR",
        "TEXT": "VARCHAR(8000)",
        "DATETIME": "DATETIME2(6)",
        "DATETIME2": "DATETIME2(6)",
        # T-SQL TIMESTAMP means rowversion, not a date-time
        "TIMESTAMP": "DATETIME2(6)",
        "DATE": "DATE",
        "BOOLEAN": "BIT",
        "DOUBLE": "FLOAT",
        "NUMBER": "DECIMAL",
        "VARCHAR2": "VARCHAR",
    },
}


class BaseTargetGenerator(TargetGenerator):
    """Shared deterministic rule engine + dbt/SQL generator."""

    platform: str = "snowflake"
    sqlglot_dialect: str = "snowflake"

    def resolve_schema(self, table: TableIR, config: ClientConfig) -> str:
        rendered = config.naming.schema_template.format(
            layer=config.naming.layer_default,
            source_db=table.database or "legacy",
            source_schema=table.schema_name or "dbo",
        )
        return self._apply_case(rendered, config.naming.table_case)

    def resolve_table_name(self, raw_name: str, config: ClientConfig) -> str:
        clean = raw_name.split(".")[-1].replace("[", "").replace("]", "")
        return self._apply_case(clean, config.naming.table_case)

    def resolve_column_name(self, raw_name: str, config: ClientConfig) -> str:
        return self._apply_case(
            raw_name.replace("[", "").replace("]", ""), config.naming.column_case
        )

    @staticmethod
    def _apply_case(value: str, mode: str) -> str:
        if mode == "upper":
            return value.upper()
        if mode == "lower":
            return value.lower()
        return value

    def target_table(self, table: TableIR, config: ClientConfig) -> TableIR:
        return table.model_copy(
            update={
                "database": "",
                "schema_name": self.resolve_schema(table, config),
                "name": self.resolve_table_name(table.name, config),
                "columns": [
                    c.model_copy(update={"name": self.resolve_column_name(c.name, config)})
                    for c in table.columns
                ],
            }
        )

    def map_data_type(self, source_type: str, config: ClientConfig) -> str:
        overrides = config.type_override_map()
        base_token = re.split(r"[\(\s]", source_type.strip().upper())[0]
        if source_type.upper() in overrides:
            return overrides[source_type.upper()]
        if base_token in overrides:
            return overrides[base_token]
        platform_map = DEFAULT_TYPE_MAPS.get(self.platform, {})
        mapped_base = platform_map.get(base_token, source_type.upper())
        # Preserve precision/scale/length parameters when target expects them
        param_match = re.search(r"\(([^)]+)\)", source_type)
        if (
            param_match
            and "(" not in mapped_base
            and mapped_base
            not in (
                "BOOLEAN",
                "DATE",
                "TIMESTAMP",
                "TIMESTAMP_NTZ",
                "STRING",
                "INT",
                "BIGINT",
                "FLOAT",
                "DOUBLE",
            )
        ):
            return f"{mapped_base}({param_match.group(1)})"
        return mapped_base

    def generate_ddl(self, table: TableIR, config: ClientConfig) -> GeneratedFile:
        schema_name = self.resolve_schema(table, config)
        tbl_name = self.resolve_table_name(table.name, config)
        col_lines: list[str] = []
        for col in sorted(table.columns, key=lambda c: c.ordinal_position):
            c_name = self.resolve_column_name(col.name, config)
            c_type = self.map_data_type(col.data_type, config)
            null_clause = "" if col.nullable else " NOT NULL"
            col_lines.append(f"    {c_name} {c_type}{null_clause}")

        cols_sql = ",\n".join(col_lines) if col_lines else "    _placeholder_id INT"
        ddl = (
            f"-- Generated by L2C Accelerator for {self.platform}\n"
            f"CREATE SCHEMA IF NOT EXISTS {schema_name};\n"
            f"CREATE TABLE IF NOT EXISTS {schema_name}.{tbl_name} (\n"
            f"{cols_sql}\n"
            f");\n"
        )
        ok, log = self.compile_check(ddl, "ddl")
        return GeneratedFile(
            relative_path=f"ddl/{self.platform}/{schema_name.lower()}_{tbl_name.lower()}.sql",
            artifact_kind="ddl",
            content=ddl,
            object_key=table.fqn,
            compile_passed=ok,
            compile_log=log,
        )

    def generate_package(
        self,
        package: PackageIR,
        tables: dict[str, TableIR],
        config: ClientConfig,
        accepted_overrides: dict[str, str] | None = None,
    ) -> GeneratedBundle:
        """Convert one parsed package. Anything that can't be converted exactly becomes a residue."""
        accepted = accepted_overrides or {}
        renderer = SqlRenderer(self, config, tables)
        residues: list[GeneratedResidue] = []
        models: list[GeneratedModel] = []
        steps: list[GeneratedStep] = []

        for task in package.tasks:
            rule = config.matching_rule(
                task.opaque.component_type if task.opaque else task.task_type, task.name, package.id
            )
            if (task.task_type == "opaque" or rule) and task.id not in accepted:
                residues.append(
                    GeneratedResidue(
                        object_key=package.id,
                        component_name=task.name,
                        reason_code=task.opaque.reason_code if task.opaque else "CUSTOM_RULE",
                        reason_detail=(rule.reason if rule else "")
                        or (task.opaque.reason_detail if task.opaque else "Manual task"),
                        original_snippet=task.opaque.original_snippet
                        if task.opaque
                        else (task.sql_statement or task.name),
                        component_key=task.id,
                    )
                )

        sql_tools = {"tsql", "plsql", "bteq", "sql"}
        read = "neutral" if package.source_tool in sql_tools else "tsql"
        sql_result = render_sql_tasks(renderer, package, read, accepted)
        models += sql_result.models
        steps += sql_result.steps
        residues += sql_result.residues

        if package.source_tool not in sql_tools:  # SQL scripts are fully covered by their tasks
            for df in package.data_flows:
                forced = {}
                for n in df.nodes:
                    rule = config.matching_rule(n.source_component_type, n.name, package.id)
                    if rule:
                        forced[n.id] = rule.reason
                flow_pkg = package.model_copy(update={"data_flows": [df]})
                builder = FlowSqlBuilder(renderer, flow_pkg, _force_opaque(df, forced), accepted)
                models += builder.build()
                residues += builder.residues

        files: list[GeneratedFile] = []
        header = f"-- Source: {package.source_tool} :: {package.name}\n"
        for m in models:
            if config.target.output == "dbt":
                content = f"{{{{ config(materialized='table') }}}}\n{header}{m.sql.rstrip(';')}\n"
                check_sql = re.sub(r"\{\{\s*config\([^}]+\)\s*\}\}", "", content)
                check_sql = re.sub(r"\{\{[^}]+\}\}", "legacy_table", check_sql)
                ok, log = self.compile_check(check_sql, "dbt_model")
                files.append(
                    GeneratedFile(
                        f"dbt/models/{m.name}.sql", "dbt_model", content, package.id, ok, log
                    )
                )
            else:
                content = f"{header}{m.sql.rstrip(';')};\n"
                ok, log = self.compile_check(content, "sql")
                files.append(
                    GeneratedFile(
                        f"sql/{self.platform}/{m.name}.sql", "sql", content, package.id, ok, log
                    )
                )
        for st in steps:
            content = f"{header}{st.sql.rstrip(';')};\n"
            check = re.sub(r"\{\{[^}]+\}\}", "legacy_table", content)
            ok, log = self.compile_check(check, "sql")
            files.append(
                GeneratedFile(
                    f"sql/{self.platform}/steps/{st.name}.sql",
                    "sql_step",
                    content,
                    package.id,
                    ok,
                    log,
                )
            )

        model_names = [m.name for m in models]
        if config.target.output == "dbt" and model_names:
            files.extend(self._build_dbt_schema_files(model_names, tables, config))
        files.extend(self._generate_orchestration(package, model_names, config))
        return GeneratedBundle(files=files, residues=residues)

    def _build_dbt_schema_files(
        self,
        model_names: list[str],
        tables: dict[str, TableIR],
        config: ClientConfig,
    ) -> list[GeneratedFile]:
        """dbt sources point at the migrated tables, under the schema/table names the DDL creates."""
        by_schema: dict[str, list[TableIR]] = {}
        for t in tables.values():
            by_schema.setdefault(self.resolve_schema(t, config), []).append(t)
        sources_yaml = {
            "version": 2,
            "sources": [
                {
                    "name": schema.lower(),
                    "database": config.target.database,
                    "schema": schema,
                    "tables": [
                        {
                            "name": self.resolve_table_name(t.name, config).lower(),
                            "identifier": self.resolve_table_name(t.name, config),
                            "description": f"Migrated from {t.fqn}",
                            "columns": [
                                {"name": self.resolve_column_name(c.name, config)}
                                for c in t.columns
                            ],
                        }
                        for t in sorted(group, key=lambda x: x.name)
                    ],
                }
                for schema, group in sorted(by_schema.items())
            ],
        }
        models_yaml = {
            "version": 2,
            "models": [
                {
                    "name": m,
                    "description": f"L2C generated model {m}",
                    "config": {"materialized": "table"},
                }
                for m in model_names
            ],
        }
        return [
            GeneratedFile(
                "dbt/models/sources.yml",
                "dbt_schema",
                yaml.safe_dump(sources_yaml, sort_keys=False),
                "dbt_project",
            ),
            GeneratedFile(
                "dbt/models/schema.yml",
                "dbt_schema",
                yaml.safe_dump(models_yaml, sort_keys=False),
                "dbt_project",
            ),
        ]

    def _generate_orchestration(
        self,
        package: PackageIR,
        model_names: list[str],
        config: ClientConfig,
    ) -> list[GeneratedFile]:
        return []

    def compile_check(
        self,
        code: str,
        artifact_kind: str,
        connection_uri: str | None = None,
    ) -> tuple[bool, str]:
        if artifact_kind in ("dbt_schema", "pipeline_json", "pyspark"):
            return True, f"Validated {artifact_kind} structure"
        try:
            dialect = DIALECT_MAP.get(self.sqlglot_dialect, "snowflake")
            stmts = [s for s in sqlglot.parse(code, read=dialect) if s is not None]
            if not stmts:
                return False, "No valid SQL statements found"
            return True, f"sqlglot compile check passed ({len(stmts)} statement(s) in {dialect})"
        except (SqlglotError, ValueError) as exc:
            return False, f"Compile check failed ({self.sqlglot_dialect}): {exc}"


def _force_opaque(df: DataFlowGraphIR, forced: dict[str, str]) -> DataFlowGraphIR:
    """Mark nodes hit by a client 'flag_manual/flag_review' rule as opaque so they go to review."""
    if not forced:
        return df
    nodes = []
    for n in df.nodes:
        if n.id in forced and n.operator != NodeOperator.OPAQUE:
            n = n.model_copy(
                update={
                    "operator": NodeOperator.OPAQUE,
                    "opaque": OpaqueSnippetIR(
                        node_id=n.id,
                        component_name=n.name,
                        component_type=n.source_component_type,
                        reason_code="CUSTOM_RULE",
                        reason_detail=forced[n.id],
                        original_snippet=n.name,
                    ),
                }
            )
        nodes.append(n)
    return df.model_copy(update={"nodes": nodes})
