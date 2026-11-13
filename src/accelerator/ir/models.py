"""Pydantic models for the three-layer Neutral IR + Database Catalog IR."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class NodeOperator(StrEnum):
    SOURCE = "Source"
    FILTER = "Filter"
    EXPRESSION = "Expression"
    LOOKUP = "Lookup"
    JOIN = "Join"
    AGGREGATE = "Aggregate"
    ROUTER = "Router"
    UNION = "Union"
    SORT = "Sort"
    DEDUPLICATE = "Deduplicate"
    TARGET = "Target"
    OPAQUE = "Opaque"


class ColumnIR(BaseModel):
    name: str
    data_type: str
    length: int | None = None
    precision: int | None = None
    scale: int | None = None
    nullable: bool = True
    is_primary_key: bool = False
    is_foreign_key: bool = False
    fk_target: str | None = None
    default_expr: str | None = None
    ordinal_position: int = 0


class TableIR(BaseModel):
    database: str = ""
    schema_name: str = "dbo"
    name: str
    object_type: str = "TABLE"  # TABLE | VIEW | PROCEDURE
    columns: list[ColumnIR] = Field(default_factory=list)
    row_count: int | None = None
    size_bytes: int | None = None
    source_sql: str | None = None
    usage_reads_30d: int = 0
    is_dead_candidate: bool = False

    @property
    def fqn(self) -> str:
        if self.database:
            return f"{self.database}.{self.schema_name}.{self.name}"
        return f"{self.schema_name}.{self.name}"


class ViewIR(BaseModel):
    database: str = ""
    schema_name: str = "dbo"
    name: str
    definition_sql: str
    neutral_sql: str = ""
    referenced_tables: list[str] = Field(default_factory=list)
    opaque_items: list[OpaqueSnippetIR] = Field(default_factory=list)


class ProcedureIR(BaseModel):
    database: str = ""
    schema_name: str = "dbo"
    name: str
    source_dialect: str = "tsql"
    definition_sql: str
    statements_neutral: list[str] = Field(default_factory=list)
    referenced_tables: list[str] = Field(default_factory=list)
    target_tables: list[str] = Field(default_factory=list)
    has_dynamic_sql: bool = False
    has_cursors: bool = False
    opaque_items: list[OpaqueSnippetIR] = Field(default_factory=list)


class CatalogIR(BaseModel):
    source_system: str
    database_name: str
    tables: list[TableIR] = Field(default_factory=list)
    views: list[ViewIR] = Field(default_factory=list)
    procedures: list[ProcedureIR] = Field(default_factory=list)


class OpaqueSnippetIR(BaseModel):
    node_id: str
    component_name: str
    component_type: str
    reason_code: str
    reason_detail: str
    original_snippet: str
    location: str = ""


class ExpressionIR(BaseModel):
    id: str
    target_column: str | None = None
    original_expr: str
    source_dialect: str = "neutral"
    neutral_sql: str = ""
    ast_json: dict[str, Any] = Field(default_factory=dict)
    is_opaque: bool = False
    opaque_reason: str | None = None


class NodeIR(BaseModel):
    id: str
    name: str
    operator: NodeOperator
    source_component_type: str = ""
    table_ref: str | None = None
    sql_query: str | None = None
    condition: ExpressionIR | None = None
    expressions: list[ExpressionIR] = Field(default_factory=list)
    join_type: str | None = None  # INNER | LEFT | RIGHT | FULL
    join_keys: list[dict[str, str]] = Field(default_factory=list)
    group_by_columns: list[str] = Field(default_factory=list)
    sort_keys: list[dict[str, str]] = Field(default_factory=list)
    dedup_keys: list[str] = Field(default_factory=list)
    routes: list[dict[str, str]] = Field(default_factory=list)
    output_columns: list[ColumnIR] = Field(default_factory=list)
    opaque: OpaqueSnippetIR | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EdgeIR(BaseModel):
    id: str
    source_node_id: str
    target_node_id: str
    branch_name: str = "default"
    # Column-level wiring {"from": upstream column, "to": downstream column}. Used when a tool
    # renames columns between steps (Informatica ports, DataStage links).
    column_lineage: list[dict[str, str]] = Field(default_factory=list)
    # "keep": pass every upstream column on, adding the renamed ones.
    # "strict": pass only the wired columns (the downstream step sees nothing else).
    projection: str = "keep"


class DataFlowGraphIR(BaseModel):
    id: str
    name: str
    nodes: list[NodeIR] = Field(default_factory=list)
    edges: list[EdgeIR] = Field(default_factory=list)

    def source_tables(self) -> list[str]:
        out: list[str] = []
        for n in self.nodes:
            if n.operator not in (NodeOperator.SOURCE, NodeOperator.LOOKUP):
                continue
            out += [str(t) for t in n.metadata.get("source_tables") or []]  # query-based sources
            if n.table_ref:
                out.append(n.table_ref)
        return list(dict.fromkeys(out))

    def target_tables(self) -> list[str]:
        return [
            n.table_ref for n in self.nodes if n.operator == NodeOperator.TARGET and n.table_ref
        ]


class VariableIR(BaseModel):
    name: str
    data_type: str = "string"
    default_value: Any = None
    namespace: str = "User"


class ParameterIR(BaseModel):
    name: str
    data_type: str = "string"
    default_value: Any = None
    required: bool = False


class ScheduleIR(BaseModel):
    name: str
    cron: str | None = None
    description: str = ""


class TaskDependencyIR(BaseModel):
    upstream_task_id: str
    downstream_task_id: str
    condition_type: str = "success"  # success | failure | completion | expression
    condition_expr: str | None = None


class OrchestrationTaskIR(BaseModel):
    id: str
    name: str
    task_type: str  # data_flow | execute_sql | script_task | container | sequence | opaque
    data_flow_id: str | None = None
    sql_statement: str | None = None
    loop_expr: str | None = None
    condition_expr: str | None = None
    children: list[OrchestrationTaskIR] = Field(default_factory=list)
    opaque: OpaqueSnippetIR | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PackageIR(BaseModel):
    """Unified neutral IR for an ETL job, workflow, or SQL script."""

    id: str
    name: str
    source_tool: str  # ssis | informatica | datastage | tsql | plsql | bteq
    source_artifact_name: str
    data_flows: list[DataFlowGraphIR] = Field(default_factory=list)
    tasks: list[OrchestrationTaskIR] = Field(default_factory=list)
    task_dependencies: list[TaskDependencyIR] = Field(default_factory=list)
    variables: list[VariableIR] = Field(default_factory=list)
    parameters: list[ParameterIR] = Field(default_factory=list)
    schedules: list[ScheduleIR] = Field(default_factory=list)
    opaque_items: list[OpaqueSnippetIR] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
