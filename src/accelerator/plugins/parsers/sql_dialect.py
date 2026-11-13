"""SQL dialect parsers (T-SQL, PL/SQL, Teradata BTEQ) producing Neutral IR via sqlglot."""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from accelerator.ir.expressions import DIALECT_MAP, ExpressionConverter
from accelerator.ir.models import (
    DataFlowGraphIR,
    EdgeIR,
    NodeIR,
    NodeOperator,
    OpaqueSnippetIR,
    OrchestrationTaskIR,
    PackageIR,
    TaskDependencyIR,
)
from accelerator.plugins.interfaces import Parser, PluginManifest

_PROCEDURAL = re.compile(
    r"\s*(?:(?:CREATE(?:\s+OR\s+REPLACE)?|REPLACE)\s+(?:EDITIONABLE\s+|NONEDITIONABLE\s+)?"
    r"(?:PROCEDURE|FUNCTION|PACKAGE|TRIGGER|TYPE|MACRO)\b|DECLARE\b|BEGIN\b(?!\s+TRANSACTION))",
    re.IGNORECASE,
)
# BTEQ / SQL*Plus commands that only affect the session or output formatting.
_HARMLESS_COMMANDS = re.compile(
    r"^\.?(?:LOGON|LOGOFF|QUIT|EXIT|SET|SESSIONS?|WIDTH|TITLEDASHES|RETLIMIT|ECHOREQ|FOLDLINE|"
    r"SEPARATOR|DEFAULTS|SHOW|COMPILE|REM(?:ARK)?|PROMPT|SPOOL|COLUMN|TTITLE|BTITLE|"
    r"WHENEVER\s+SQLERROR\s+EXIT|WHENEVER\s+OSERROR\s+EXIT)\b",
    re.IGNORECASE,
)
_STOP_ON_ERROR = re.compile(
    r"^\.IF\s+ERRORCODE\s*(?:<>|!=|>|NE|GT)\s*0\s+THEN\s+\.?(?:QUIT|EXIT)\b", re.IGNORECASE
)
# Statements with no effect on the target (statistics, transaction brackets, default database).
_NO_EFFECT = re.compile(
    r"^\s*(?:COLLECT\s+STAT(?:ISTIC)?S?\b|BT\s*$|ET\s*$|BEGIN\s+TRANSACTION\s*$|"
    r"END\s+TRANSACTION\s*$|COMMIT(?:\s+WORK)?\s*$|DATABASE\s+\w+\s*$)",
    re.IGNORECASE,
)


def _prepare_statements(
    text: str, source_tool: str, dialect: str
) -> tuple[list[str], list[tuple[str, str, str]]]:
    """Separate script commands from SQL; split SQL into statements, keeping blocks whole.

    Returns (statements, notices) where notices are (reason_code, detail, snippet) for script
    commands a person must review.
    """
    from accelerator.plugins.collectors.ddl_reader import split_statements

    notices: list[tuple[str, str, str]] = []
    sql_lines: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        is_bteq_cmd = source_tool == "bteq" and s.startswith(".")
        is_sqlplus_cmd = source_tool == "plsql" and (
            s.startswith("@")
            or (
                re.match(
                    r"(?:SET|PROMPT|SPOOL|WHENEVER|REM|REMARK|COLUMN|TTITLE|BTITLE|EXEC|"
                    r"EXECUTE)\b(?!\s+IMMEDIATE)",
                    s,
                    re.IGNORECASE,
                )
                and not re.match(r"SET\s+TRANSACTION\b", s, re.IGNORECASE)
            )
        )
        if is_bteq_cmd or is_sqlplus_cmd:
            if _STOP_ON_ERROR.match(s) or _HARMLESS_COMMANDS.match(s):
                continue  # stopping on the first error is how converted steps run anyway
            code = "BTEQ_COMMAND" if is_bteq_cmd else "SQLPLUS_COMMAND"
            notices.append((code, f"Script command needs review: {s.split()[0]}", s))
            continue
        sql_lines.append(line)
    script = "\n".join(sql_lines)
    if source_tool == "tsql":
        script = re.sub(r"(?im)^\s*GO\s*$", ";", script)
    statements = []
    for stmt in split_statements(script, dialect if dialect != "tsql" else "tsql"):
        if _NO_EFFECT.match(stmt):
            continue
        if dialect == "teradata":
            stmt = re.sub(
                r"^\s*DEL(?:ETE)?\s+FROM\s+(\S+)\s+ALL\s*$",
                r"DELETE FROM \1",
                stmt,
                flags=re.IGNORECASE,
            )
        statements.append(stmt)
    return statements, notices


def _parse_sql_to_ir(artifact_name: str, sql_text: str, source_tool: str) -> PackageIR:
    dialect = DIALECT_MAP.get(source_tool.lower(), "tsql")
    flow_nodes: list[NodeIR] = []
    flow_edges: list[EdgeIR] = []
    tasks: list[OrchestrationTaskIR] = []
    task_deps: list[TaskDependencyIR] = []
    opaque_items: list[OpaqueSnippetIR] = []

    statements, notices = _prepare_statements(sql_text, source_tool, dialect)
    for n_idx, (code, detail, snippet) in enumerate(notices, start=1):
        opaque_items.append(
            OpaqueSnippetIR(
                node_id=f"script_cmd_{n_idx}",
                component_name=f"{artifact_name}#command_{n_idx}",
                component_type="ScriptCommand",
                reason_code=code,
                reason_detail=detail,
                original_snippet=snippet,
                location=artifact_name,
            )
        )
        tasks.append(
            OrchestrationTaskIR(
                id=f"script_cmd_{n_idx}",
                name=f"Script command {n_idx}",
                task_type="opaque",
                sql_statement=snippet,
                opaque=opaque_items[-1],
            )
        )
    raw_chunks = statements

    prev_task_id: str | None = None
    for idx, chunk in enumerate(raw_chunks, start=1):
        task_id = f"sql_step_{idx}"
        # Detect procedural / dynamic constructs that require review
        if _PROCEDURAL.match(chunk):
            opaque = OpaqueSnippetIR(
                node_id=task_id,
                component_name=f"{artifact_name}#step_{idx}",
                component_type="ProceduralBlock",
                reason_code="PROCEDURAL_BLOCK",
                reason_detail="Stored procedure, function, trigger, package or macro: its "
                "step-by-step logic must be redesigned (for example as models plus orchestration).",
                original_snippet=chunk,
                location=artifact_name,
            )
            opaque_items.append(opaque)
            tasks.append(
                OrchestrationTaskIR(
                    id=task_id,
                    name=f"Step {idx} (Procedural)",
                    task_type="opaque",
                    sql_statement=chunk,
                    opaque=opaque,
                )
            )
            flow_nodes.append(
                NodeIR(
                    id=task_id,
                    name=f"Procedural Step {idx}",
                    operator=NodeOperator.OPAQUE,
                    source_component_type="ProceduralSQL",
                    opaque=opaque,
                )
            )
        elif re.search(
            r"\b(?:EXEC|EXECUTE\s+IMMEDIATE|sp_executesql|CURSOR|WHILE|GOTO)\b",
            chunk,
            re.IGNORECASE,
        ):
            opaque = OpaqueSnippetIR(
                node_id=task_id,
                component_name=f"{artifact_name}#step_{idx}",
                component_type="ProceduralOrDynamicSQL",
                reason_code="PROCEDURAL_OR_DYNAMIC_SQL",
                reason_detail="Dynamic SQL, cursor, or procedural loop requires manual/AI verification.",
                original_snippet=chunk,
                location=artifact_name,
            )
            opaque_items.append(opaque)
            tasks.append(
                OrchestrationTaskIR(
                    id=task_id,
                    name=f"Step {idx} (Opaque SQL)",
                    task_type="opaque",
                    sql_statement=chunk,
                    opaque=opaque,
                )
            )
            flow_nodes.append(
                NodeIR(
                    id=task_id,
                    name=f"Opaque Step {idx}",
                    operator=NodeOperator.OPAQUE,
                    source_component_type="ProceduralSQL",
                    opaque=opaque,
                )
            )
        else:
            try:
                parsed = sqlglot.parse_one(chunk, read=dialect)
                sources = [t.sql(dialect=dialect) for t in parsed.find_all(exp.Table)]
                target_tbl: str | None = None
                if isinstance(parsed, (exp.Insert, exp.Merge, exp.Create)):
                    tgt = parsed.this
                    if isinstance(tgt, exp.Schema):
                        tgt = tgt.this
                    if tgt is not None:
                        target_tbl = tgt.sql(dialect=dialect)
                        sources = [s for s in sources if s != target_tbl]

                step_nodes: list[NodeIR] = []
                for s_idx, src_tbl in enumerate(dict.fromkeys(sources), start=1):
                    src_node = NodeIR(
                        id=f"{task_id}_src_{s_idx}",
                        name=src_tbl,
                        operator=NodeOperator.SOURCE,
                        source_component_type="SqlTable",
                        table_ref=src_tbl,
                    )
                    step_nodes.append(src_node)
                    flow_nodes.append(src_node)

                where_expr = parsed.find(exp.Where)
                filter_node: NodeIR | None = None
                if where_expr and where_expr.this:
                    cond_ir = ExpressionConverter.to_neutral(
                        f"{task_id}_filter", where_expr.this.sql(dialect=dialect), source_tool
                    )
                    filter_node = NodeIR(
                        id=f"{task_id}_flt",
                        name=f"Filter Step {idx}",
                        operator=NodeOperator.FILTER,
                        source_component_type="WhereClause",
                        condition=cond_ir,
                    )
                    flow_nodes.append(filter_node)
                    for sn in step_nodes:
                        flow_edges.append(
                            EdgeIR(
                                id=f"e_{sn.id}_{filter_node.id}",
                                source_node_id=sn.id,
                                target_node_id=filter_node.id,
                            )
                        )

                tgt_node = NodeIR(
                    id=f"{task_id}_tgt",
                    name=target_tbl or f"QueryOutput_{idx}",
                    operator=NodeOperator.TARGET,
                    source_component_type="SqlTarget",
                    table_ref=target_tbl,
                    sql_query=parsed.sql(dialect="postgres"),
                )
                flow_nodes.append(tgt_node)
                upstream_for_tgt = [filter_node] if filter_node else step_nodes
                for up in upstream_for_tgt:
                    flow_edges.append(
                        EdgeIR(
                            id=f"e_{up.id}_{tgt_node.id}",
                            source_node_id=up.id,
                            target_node_id=tgt_node.id,
                        )
                    )

                tasks.append(
                    OrchestrationTaskIR(
                        id=task_id,
                        name=f"Execute SQL Step {idx}",
                        task_type="execute_sql",
                        # kept in the source dialect: conversion needs its exact meaning
                        sql_statement=parsed.sql(dialect=dialect),
                    )
                )
            except (SqlglotError, ValueError) as exc:
                opaque = OpaqueSnippetIR(
                    node_id=task_id,
                    component_name=f"{artifact_name}#step_{idx}",
                    component_type="UnparsedSQL",
                    reason_code="SQL_PARSE_ERROR",
                    reason_detail=str(exc),
                    original_snippet=chunk,
                    location=artifact_name,
                )
                opaque_items.append(opaque)
                tasks.append(
                    OrchestrationTaskIR(
                        id=task_id,
                        name=f"Step {idx} (Unparsed)",
                        task_type="opaque",
                        sql_statement=chunk,
                        opaque=opaque,
                    )
                )
                flow_nodes.append(
                    NodeIR(
                        id=task_id,
                        name=f"Opaque Step {idx}",
                        operator=NodeOperator.OPAQUE,
                        source_component_type="UnparsedSQL",
                        opaque=opaque,
                    )
                )

        if prev_task_id is not None:
            task_deps.append(
                TaskDependencyIR(upstream_task_id=prev_task_id, downstream_task_id=task_id)
            )
        prev_task_id = task_id

    df = DataFlowGraphIR(
        id=f"{artifact_name}_df",
        name=artifact_name,
        nodes=flow_nodes,
        edges=flow_edges,
    )
    return PackageIR(
        id=artifact_name,
        name=artifact_name,
        source_tool=source_tool,
        source_artifact_name=artifact_name,
        data_flows=[df] if flow_nodes else [],
        tasks=tasks,
        task_dependencies=task_deps,
        opaque_items=opaque_items,
    )


class _BaseSqlParser(Parser):
    source_tool: str = "tsql"

    def parse_artifact(self, artifact_name: str, content_bytes: bytes) -> PackageIR:
        return _parse_sql_to_ir(
            artifact_name, content_bytes.decode("utf-8", errors="replace"), self.source_tool
        )

    def run_golden_check(self) -> tuple[bool, str]:
        sample = b"INSERT INTO dbo.tgt SELECT id, amt FROM dbo.src WHERE amt > 10;"
        pkg = self.parse_artifact("golden.sql", sample)
        ok = len(pkg.data_flows) == 1 and any(
            n.operator == NodeOperator.SOURCE for n in pkg.data_flows[0].nodes
        )
        return ok, f"{self.manifest.name} golden check passed" if ok else "Failed golden check"


class TSqlParser(_BaseSqlParser):
    source_tool = "tsql"
    manifest = PluginManifest(
        plugin_type="Parser",
        name="tsql_parser",
        version="0.1.0",
        supported_versions=["SQL Server 2016+"],
        capabilities={"ddl": True, "dml": True, "procedures": True},
    )


class PlSqlParser(_BaseSqlParser):
    source_tool = "plsql"
    manifest = PluginManifest(
        plugin_type="Parser",
        name="plsql_parser",
        version="0.1.0",
        supported_versions=["Oracle 12c+"],
        capabilities={"ddl": True, "dml": True, "procedures": True},
    )


class BteqParser(_BaseSqlParser):
    source_tool = "bteq"
    manifest = PluginManifest(
        plugin_type="Parser",
        name="bteq_parser",
        version="0.1.0",
        supported_versions=["Teradata BTEQ"],
        capabilities={"bteq": True, "dml": True},
    )


class PgSqlParser(_BaseSqlParser):
    source_tool = "postgres"
    manifest = PluginManifest(
        plugin_type="Parser",
        name="pgsql_parser",
        version="0.1.0",
        supported_versions=["PostgreSQL 12+"],
        capabilities={"ddl": True, "dml": True, "views": True},
    )
