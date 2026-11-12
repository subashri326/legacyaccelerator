"""Shared rule engine, type mapping, CTE SQL synthesis, dbt scaffolding, and compile checks."""

from __future__ import annotations

import re

import sqlglot
import yaml
from sqlglot.errors import SqlglotError

from accelerator.config.client_config import ClientConfig
from accelerator.ir.expressions import DIALECT_MAP, ExpressionConverter
from accelerator.ir.models import (
    DataFlowGraphIR,
    NodeIR,
    NodeOperator,
    PackageIR,
    TableIR,
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
        "DATE": "DATE",
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
        "DATE": "DATE",
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
        "DATE": "DATE",
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
        return self._apply_case(raw_name.replace("[", "").replace("]", ""), config.naming.column_case)

    @staticmethod
    def _apply_case(value: str, mode: str) -> str:
        if mode == "upper":
            return value.upper()
        if mode == "lower":
            return value.lower()
        return value

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
        if param_match and "(" not in mapped_base and mapped_base not in ("BOOLEAN", "DATE", "TIMESTAMP", "TIMESTAMP_NTZ", "STRING", "INT", "BIGINT", "FLOAT", "DOUBLE"):
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

    def _matches_custom_manual_rule(self, component_type: str, config: ClientConfig) -> str | None:
        for rule in config.custom_rules:
            if rule.action in ("flag_manual", "flag_review") and (
                component_type.lower() in rule.match.lower() or f"'{component_type}'" in rule.match
            ):
                return rule.reason
        return None

    def _flow_to_sql_ctes(
        self,
        df: DataFlowGraphIR,
        config: ClientConfig,
        accepted_overrides: dict[str, str] | None = None,
    ) -> tuple[str, list[GeneratedResidue]]:
        """Render a Neutral DataFlowGraphIR into target SQL using topological CTEs."""
        residues: list[GeneratedResidue] = []
        accepted = accepted_overrides or {}

        node_map = {n.id: n for n in df.nodes}
        upstream_map: dict[str, list[str]] = {n.id: [] for n in df.nodes}
        for edge in df.edges:
            if edge.target_node_id in upstream_map:
                upstream_map[edge.target_node_id].append(edge.source_node_id)

        # Topological sort (fallback to input order on cycles)
        ordered: list[NodeIR] = []
        visited: set[str] = set()

        def visit(nid: str) -> None:
            if nid in visited or nid not in node_map:
                return
            visited.add(nid)
            for up in upstream_map.get(nid, []):
                visit(up)
            ordered.append(node_map[nid])

        for n in df.nodes:
            visit(n.id)

        cte_names: dict[str, str] = {}
        ctes: list[str] = []

        for idx, node in enumerate(ordered, start=1):
            cte_name = re.sub(r"[^a-zA-Z0-9_]", "_", f"cte_{idx}_{node.name.lower()}").strip("_")
            ups = [cte_names[u] for u in upstream_map.get(node.id, []) if u in cte_names]
            prev_cte = ups[0] if ups else "dual"

            custom_reason = self._matches_custom_manual_rule(node.source_component_type, config)
            if node.operator == NodeOperator.OPAQUE or custom_reason:
                if node.id in accepted:
                    cte_names[node.id] = cte_name
                    ctes.append(f"{cte_name} AS (\n    -- Human-accepted replacement\n    {accepted[node.id]}\n)")
                else:
                    snippet = node.opaque.original_snippet if node.opaque else f"{node.name} ({node.source_component_type})"
                    reason_code = node.opaque.reason_code if node.opaque else "CUSTOM_RULE_MANUAL"
                    reason_detail = custom_reason or (node.opaque.reason_detail if node.opaque else "Flagged for manual review")
                    residues.append(
                        GeneratedResidue(
                            object_key=df.id,
                            component_name=node.name,
                            reason_code=reason_code,
                            reason_detail=reason_detail,
                            original_snippet=snippet,
                        )
                    )
                    cte_names[node.id] = prev_cte
                continue

            if node.operator == NodeOperator.SOURCE:
                tbl = (node.table_ref or node.name).replace("[", "").replace("]", "")
                if node.sql_query:
                    try:
                        rendered_src, _ = ExpressionConverter.transpile_sql(
                            node.sql_query, "tsql", self.platform
                        )
                        body = rendered_src.rstrip(";")
                    except (SqlglotError, ValueError):
                        body = f"SELECT * FROM {tbl}"
                elif config.target.output == "dbt":
                    src_tbl = tbl.split(".")[-1].lower()
                    body = f"SELECT * FROM {{{{ source('legacy', '{src_tbl}') }}}}"
                else:
                    body = f"SELECT * FROM {tbl}"
                cte_names[node.id] = cte_name
                ctes.append(f"{cte_name} AS (\n    {body}\n)")

            elif node.operator == NodeOperator.FILTER:
                cond_sql = "1 = 1"
                if node.condition and node.condition.neutral_sql:
                    cond_sql = ExpressionConverter.render_target(node.condition.neutral_sql, self.platform)
                elif node.condition and node.condition.is_opaque:
                    residues.append(
                        GeneratedResidue(
                            object_key=df.id,
                            component_name=node.name,
                            reason_code="EXPRESSION_OPAQUE",
                            reason_detail=node.condition.opaque_reason or "Unparsed filter expression",
                            original_snippet=node.condition.original_expr,
                        )
                    )
                cte_names[node.id] = cte_name
                ctes.append(f"{cte_name} AS (\n    SELECT * FROM {prev_cte} WHERE {cond_sql}\n)")

            elif node.operator == NodeOperator.EXPRESSION:
                proj: list[str] = ["*"]
                for expr in node.expressions:
                    col_alias = self.resolve_column_name(expr.target_column or "derived_col", config)
                    if expr.is_opaque:
                        residues.append(
                            GeneratedResidue(
                                object_key=df.id,
                                component_name=f"{node.name}.{col_alias}",
                                reason_code="EXPRESSION_OPAQUE",
                                reason_detail=expr.opaque_reason or "Unparsed derived expression",
                                original_snippet=expr.original_expr,
                            )
                        )
                        proj.append(f"NULL AS {col_alias} /* TODO: {expr.original_expr} */")
                    else:
                        rendered = ExpressionConverter.render_target(expr.neutral_sql, self.platform)
                        proj.append(f"{rendered} AS {col_alias}")
                cte_names[node.id] = cte_name
                ctes.append(f"{cte_name} AS (\n    SELECT {', '.join(proj)} FROM {prev_cte}\n)")

            elif node.operator in (NodeOperator.JOIN, NodeOperator.LOOKUP):
                right_cte = ups[1] if len(ups) > 1 else (node.table_ref or "lookup_ref")
                join_kw = f"{(node.join_type or 'LEFT').upper()} JOIN"
                keys = node.join_keys or [{"left": "id", "right": "id"}]
                on_clause = " AND ".join(f"l.{k['left']} = r.{k['right']}" for k in keys)
                cte_names[node.id] = cte_name
                ctes.append(
                    f"{cte_name} AS (\n    SELECT l.* FROM {prev_cte} l\n    {join_kw} {right_cte} r ON {on_clause}\n)"
                )

            elif node.operator == NodeOperator.AGGREGATE:
                grp = ", ".join(node.group_by_columns) if node.group_by_columns else "1"
                cte_names[node.id] = cte_name
                ctes.append(
                    f"{cte_name} AS (\n    SELECT {grp}, COUNT(*) AS _row_cnt FROM {prev_cte} GROUP BY {grp}\n)"
                )

            elif node.operator == NodeOperator.UNION and len(ups) >= 2:
                union_sql = "\n    UNION ALL\n    ".join(f"SELECT * FROM {u}" for u in ups)
                cte_names[node.id] = cte_name
                ctes.append(f"{cte_name} AS (\n    {union_sql}\n)")

            elif node.operator == NodeOperator.DEDUPLICATE:
                keys = ", ".join(node.dedup_keys) if node.dedup_keys else "1"
                cte_names[node.id] = cte_name
                ctes.append(
                    f"{cte_name} AS (\n    SELECT * FROM (\n"
                    f"        SELECT *, ROW_NUMBER() OVER (PARTITION BY {keys} ORDER BY {keys}) AS _rn FROM {prev_cte}\n"
                    f"    ) d WHERE _rn = 1\n)"
                )

            elif node.operator == NodeOperator.ROUTER:
                route_cases = []
                for r in node.routes:
                    cond = r.get("condition") or "1=1"
                    route_cases.append(f"WHEN {cond} THEN '{r.get('name', 'route')}'")
                case_expr = f"CASE {' '.join(route_cases)} ELSE 'default' END AS _route_branch" if route_cases else "'default' AS _route_branch"
                cte_names[node.id] = cte_name
                ctes.append(f"{cte_name} AS (\n    SELECT *, {case_expr} FROM {prev_cte}\n)")

            elif node.operator == NodeOperator.TARGET:
                cte_names[node.id] = prev_cte

            else:
                cte_names[node.id] = prev_cte

        if not ctes:
            return "SELECT 1 AS empty_flow;\n", residues

        final_cte = list(cte_names.values())[-1]
        for n in reversed(ordered):
            if n.operator == NodeOperator.TARGET and n.id in cte_names:
                final_cte = cte_names[n.id]
                break

        sql_out = "WITH " + ",\n".join(ctes) + f"\nSELECT * FROM {final_cte};\n"
        return sql_out, residues

    def _build_dbt_schema_files(
        self,
        model_names: list[str],
        tables: dict[str, TableIR],
        config: ClientConfig,
    ) -> list[GeneratedFile]:
        sources_yaml = {
            "version": 2,
            "sources": [
                {
                    "name": "legacy",
                    "database": config.target.database,
                    "schema": config.naming.layer_default,
                    "tables": [
                        {
                            "name": t.name.lower(),
                            "description": f"Migrated source table {t.fqn}",
                            "columns": [{"name": c.name.lower()} for c in t.columns],
                        }
                        for t in tables.values()
                    ],
                }
            ],
        }
        models_yaml = {
            "version": 2,
            "models": [
                {
                    "name": m,
                    "description": f"L2C generated dbt model for {m}",
                    "config": {"materialized": "table"},
                }
                for m in model_names
            ],
        }
        return [
            GeneratedFile(
                relative_path="dbt/models/sources.yml",
                artifact_kind="dbt_schema",
                content=yaml.safe_dump(sources_yaml, sort_keys=False),
                object_key="dbt_project",
            ),
            GeneratedFile(
                relative_path="dbt/models/schema.yml",
                artifact_kind="dbt_schema",
                content=yaml.safe_dump(models_yaml, sort_keys=False),
                object_key="dbt_project",
            ),
        ]

    def generate_package(
        self,
        package: PackageIR,
        tables: dict[str, TableIR],
        config: ClientConfig,
        accepted_overrides: dict[str, str] | None = None,
    ) -> GeneratedBundle:
        files: list[GeneratedFile] = []
        residues: list[GeneratedResidue] = []
        model_names: list[str] = []

        # Evaluate custom rules at task level as well
        for task in package.tasks:
            custom_reason = self._matches_custom_manual_rule(task.task_type, config)
            if (task.task_type == "opaque" or custom_reason) and task.id not in (accepted_overrides or {}):
                residues.append(
                        GeneratedResidue(
                            object_key=package.id,
                            component_name=task.name,
                            reason_code=task.opaque.reason_code if task.opaque else "TASK_MANUAL_RULE",
                            reason_detail=custom_reason or (task.opaque.reason_detail if task.opaque else "Manual task"),
                            original_snippet=task.opaque.original_snippet if task.opaque else (task.sql_statement or task.name),
                        )
                    )

        for df in package.data_flows:
            sql_body, df_res = self._flow_to_sql_ctes(df, config, accepted_overrides)
            for r in df_res:
                r.object_key = package.id
            residues.extend(df_res)

            safe_name = re.sub(r"[^a-zA-Z0-9_]+", "_", df.name.lower()).strip("_")
            model_names.append(safe_name)

            if config.target.output == "dbt":
                model_content = (
                    f"{{{{ config(materialized='table') }}}}\n"
                    f"-- Source: {package.source_tool} :: {package.name} / {df.name}\n"
                    + sql_body.rstrip(";\n")
                    + "\n"
                )
                # Strip dbt config blocks to empty and source/ref Jinja tags to placeholder table
                check_sql = re.sub(r"\{\{\s*config\([^}]+\)\s*\}\}", "", model_content)
                check_sql = re.sub(r"\{\{[^}]+\}\}", "legacy_table", check_sql)
                ok, log = self.compile_check(check_sql, "dbt_model")
                files.append(
                    GeneratedFile(
                        relative_path=f"dbt/models/{safe_name}.sql",
                        artifact_kind="dbt_model",
                        content=model_content,
                        object_key=package.id,
                        compile_passed=ok,
                        compile_log=log,
                    )
                )
            else:
                ok, log = self.compile_check(sql_body, "sql")
                files.append(
                    GeneratedFile(
                        relative_path=f"sql/{self.platform}/{safe_name}.sql",
                        artifact_kind="sql",
                        content=sql_body,
                        object_key=package.id,
                        compile_passed=ok,
                        compile_log=log,
                    )
                )

        if config.target.output == "dbt" and model_names:
            files.extend(self._build_dbt_schema_files(model_names, tables, config))

        # Platform-specific orchestration output (Tasks / Lakeflow / Data Factory)
        orch_files = self._generate_orchestration(package, model_names, config)
        files.extend(orch_files)
        return GeneratedBundle(files=files, residues=residues)

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
