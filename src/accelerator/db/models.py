"""Complete 10-group Metadata Database schema (Section 5.2)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from accelerator.db.base import AuditMixin, Base, JsonType, new_uuid, utcnow

# ---------------------------------------------------------------------------
# 1. Engagement: client, project, environment, connection, config_version
# ---------------------------------------------------------------------------


class Client(Base, AuditMixin):
    __tablename__ = "client"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    industry: Mapped[str] = mapped_column(String(100), default="general", nullable=False)

    projects: Mapped[list[Project]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )


class Project(Base, AuditMixin):
    __tablename__ = "project"
    __table_args__ = (UniqueConstraint("client_id", "slug", name="uq_project_client_slug"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("client.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_platform: Mapped[str] = mapped_column(String(50), nullable=False)
    # A project can use several ETL tools at once (e.g. SSIS + Informatica + DataStage)
    etl_tools: Mapped[list[str]] = mapped_column(JsonType, default=list, nullable=False)
    # Empty while the client is still comparing targets
    target_platform: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(
        String(40), default="active", nullable=False
    )  # comparing | active | closed

    client: Mapped[Client] = relationship(back_populates="projects")
    environments: Mapped[list[Environment]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    config_versions: Mapped[list[ConfigVersion]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Environment(Base, AuditMixin):
    __tablename__ = "environment"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_env_project_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)  # dev | test | prod
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    project: Mapped[Project] = relationship(back_populates="environments")


class Connection(Base, AuditMixin):
    """A source or target system.

    Saved connections belong to a client (``client_id`` set, ``project_id`` empty) and are
    reused by its migrations; a project's own copy names the connection its config uses.
    """

    __tablename__ = "connection"
    __table_args__ = (
        UniqueConstraint("project_id", "environment_id", "name", name="uq_conn_project_env_name"),
        UniqueConstraint("client_id", "name", name="uq_conn_client_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=True
    )
    client_id: Mapped[str | None] = mapped_column(
        ForeignKey("client.id", ondelete="CASCADE"), nullable=True
    )
    environment_id: Mapped[str | None] = mapped_column(
        ForeignKey("environment.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False)  # source | target | git | storage
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    secret_ref: Mapped[str] = mapped_column(String(255), nullable=False)  # vault:// or env://
    properties: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    # Result of the last "Test connection": passed | failed (empty = never tested).
    last_test_status: Mapped[str | None] = mapped_column(String(20))
    last_test_message: Mapped[str | None] = mapped_column(Text)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConfigVersion(Base, AuditMixin):
    __tablename__ = "config_version"
    __table_args__ = (UniqueConstraint("project_id", "version_num", name="uq_cfg_project_ver"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    version_num: Mapped[int] = mapped_column(Integer, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_json: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    project: Mapped[Project] = relationship(back_populates="config_versions")


# ---------------------------------------------------------------------------
# 2. Inventory: source_artifact, db_object, column, etl_job, etl_component
# ---------------------------------------------------------------------------


class SourceArtifact(Base, AuditMixin):
    __tablename__ = "source_artifact"
    __table_args__ = (
        UniqueConstraint("project_id", "artifact_key", "content_hash", name="uq_artifact_ver"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    artifact_key: Mapped[str] = mapped_column(String(300), nullable=False)
    artifact_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # dtsx | informatica_xml | dsx | sql | catalog
    source_tool: Mapped[str] = mapped_column(String(50), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    version_num: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    is_latest: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class DbObject(Base, AuditMixin):
    __tablename__ = "db_object"
    __table_args__ = (UniqueConstraint("project_id", "fqn", name="uq_db_obj_fqn"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    source_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_artifact.id", ondelete="SET NULL")
    )
    database_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    schema_name: Mapped[str] = mapped_column(String(120), default="dbo", nullable=False)
    object_name: Mapped[str] = mapped_column(String(200), nullable=False)
    fqn: Mapped[str] = mapped_column(String(400), nullable=False)
    object_type: Mapped[str] = mapped_column(String(40), nullable=False)  # TABLE | VIEW | PROCEDURE
    row_count: Mapped[int | None] = mapped_column(BigInteger)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    usage_reads_30d: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_dead_candidate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    definition_sql: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    # e.g. {"key_source": "UNIQUE PRIMARY INDEX"}: details that change how a table is handled
    attributes: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)

    columns: Mapped[list[DbColumn]] = relationship(
        back_populates="db_object", cascade="all, delete-orphan"
    )


class DbColumn(Base, AuditMixin):
    __tablename__ = "db_column"
    __table_args__ = (UniqueConstraint("db_object_id", "column_name", name="uq_db_col_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    db_object_id: Mapped[str] = mapped_column(
        ForeignKey("db_object.id", ondelete="CASCADE"), nullable=False
    )
    column_name: Mapped[str] = mapped_column(String(200), nullable=False)
    data_type: Mapped[str] = mapped_column(String(100), nullable=False)
    length: Mapped[int | None] = mapped_column(Integer)
    precision: Mapped[int | None] = mapped_column(Integer)
    scale: Mapped[int | None] = mapped_column(Integer)
    nullable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_primary_key: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_foreign_key: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    fk_target: Mapped[str | None] = mapped_column(String(400))
    ordinal_position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # e.g. {"case_insensitive": true, "unicode": true}: behaviour the type alone doesn't say
    attributes: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)

    db_object: Mapped[DbObject] = relationship(back_populates="columns")


class EtlJob(Base, AuditMixin):
    __tablename__ = "etl_job"
    __table_args__ = (UniqueConstraint("project_id", "job_name", name="uq_etl_job_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    source_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_artifact.id", ondelete="SET NULL")
    )
    job_name: Mapped[str] = mapped_column(String(300), nullable=False)
    source_tool: Mapped[str] = mapped_column(String(50), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    run_frequency: Mapped[str] = mapped_column(String(50), default="unknown", nullable=False)
    last_executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_dead_candidate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    components: Mapped[list[EtlComponent]] = relationship(
        back_populates="etl_job", cascade="all, delete-orphan"
    )


class EtlComponent(Base, AuditMixin):
    __tablename__ = "etl_component"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    etl_job_id: Mapped[str] = mapped_column(
        ForeignKey("etl_job.id", ondelete="CASCADE"), nullable=False
    )
    component_name: Mapped[str] = mapped_column(String(255), nullable=False)
    component_type: Mapped[str] = mapped_column(String(120), nullable=False)
    raw_properties: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)

    etl_job: Mapped[EtlJob] = relationship(back_populates="components")


# ---------------------------------------------------------------------------
# 3. Neutral Model: ir_flow, ir_node, ir_edge, ir_expression, ir_task
# ---------------------------------------------------------------------------


class IrFlow(Base, AuditMixin):
    __tablename__ = "ir_flow"
    __table_args__ = (UniqueConstraint("project_id", "flow_key", name="uq_ir_flow_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    etl_job_id: Mapped[str | None] = mapped_column(ForeignKey("etl_job.id", ondelete="CASCADE"))
    db_object_id: Mapped[str | None] = mapped_column(ForeignKey("db_object.id", ondelete="CASCADE"))
    flow_key: Mapped[str] = mapped_column(String(300), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    source_tool: Mapped[str] = mapped_column(String(50), nullable=False)
    # Which version of the source this flow was parsed from, so a changed source is detected
    source_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_artifact.id", ondelete="SET NULL")
    )
    source_content_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    ir_payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)

    nodes: Mapped[list[IrNode]] = relationship(back_populates="flow", cascade="all, delete-orphan")
    edges: Mapped[list[IrEdge]] = relationship(back_populates="flow", cascade="all, delete-orphan")
    tasks: Mapped[list[IrTask]] = relationship(back_populates="flow", cascade="all, delete-orphan")


class IrNode(Base, AuditMixin):
    __tablename__ = "ir_node"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    ir_flow_id: Mapped[str] = mapped_column(
        ForeignKey("ir_flow.id", ondelete="CASCADE"), nullable=False
    )
    node_key: Mapped[str] = mapped_column(String(150), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    operator: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # Source | Filter | ... | Opaque
    table_ref: Mapped[str | None] = mapped_column(String(400))
    is_opaque: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    opaque_reason: Mapped[str | None] = mapped_column(String(255))
    node_payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)

    flow: Mapped[IrFlow] = relationship(back_populates="nodes")


class IrEdge(Base, AuditMixin):
    __tablename__ = "ir_edge"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    ir_flow_id: Mapped[str] = mapped_column(
        ForeignKey("ir_flow.id", ondelete="CASCADE"), nullable=False
    )
    source_node_key: Mapped[str] = mapped_column(String(150), nullable=False)
    target_node_key: Mapped[str] = mapped_column(String(150), nullable=False)
    branch_name: Mapped[str] = mapped_column(String(100), default="default", nullable=False)

    flow: Mapped[IrFlow] = relationship(back_populates="edges")


class IrExpression(Base, AuditMixin):
    __tablename__ = "ir_expression"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    ir_flow_id: Mapped[str] = mapped_column(
        ForeignKey("ir_flow.id", ondelete="CASCADE"), nullable=False
    )
    ir_node_id: Mapped[str | None] = mapped_column(ForeignKey("ir_node.id", ondelete="CASCADE"))
    target_column: Mapped[str | None] = mapped_column(String(200))
    original_expr: Mapped[str] = mapped_column(Text, nullable=False)
    neutral_sql: Mapped[str] = mapped_column(Text, default="", nullable=False)
    ast_json: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    is_opaque: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    opaque_reason: Mapped[str | None] = mapped_column(String(255))


class IrTask(Base, AuditMixin):
    __tablename__ = "ir_task"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    ir_flow_id: Mapped[str] = mapped_column(
        ForeignKey("ir_flow.id", ondelete="CASCADE"), nullable=False
    )
    task_key: Mapped[str] = mapped_column(String(150), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    task_type: Mapped[str] = mapped_column(String(60), nullable=False)
    depends_on: Mapped[list[str]] = mapped_column(JsonType, default=list, nullable=False)
    task_payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)

    flow: Mapped[IrFlow] = relationship(back_populates="tasks")


# ---------------------------------------------------------------------------
# 4. Lineage: dependency
# ---------------------------------------------------------------------------


class Dependency(Base, AuditMixin):
    __tablename__ = "dependency"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "upstream_key", "downstream_key", "edge_type", name="uq_dep_edge"
        ),
        Index("ix_dep_project", "project_id", "upstream_key", "downstream_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    upstream_key: Mapped[str] = mapped_column(String(400), nullable=False)
    downstream_key: Mapped[str] = mapped_column(String(400), nullable=False)
    edge_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # reads | writes | calls | triggers | fk
    column_lineage: Mapped[list[dict[str, str]]] = mapped_column(
        JsonType, default=list, nullable=False
    )


# ---------------------------------------------------------------------------
# 5. Assessment: complexity_score, effort_estimate, wave, wave_member
# ---------------------------------------------------------------------------


class ComplexityScore(Base, AuditMixin):
    __tablename__ = "complexity_score"
    __table_args__ = (UniqueConstraint("project_id", "object_key", name="uq_complexity_obj"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(400), nullable=False)
    object_kind: Mapped[str] = mapped_column(
        String(40), nullable=False
    )  # table | view | procedure | etl_job
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    classification: Mapped[str] = mapped_column(
        String(30), nullable=False
    )  # auto_convertible | needs_review | manual
    opaque_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JsonType, default=list, nullable=False)
    config_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("config_version.id", ondelete="SET NULL")
    )


class EffortEstimate(Base, AuditMixin):
    __tablename__ = "effort_estimate"
    __table_args__ = (
        UniqueConstraint("project_id", "object_key", "target_platform", name="uq_effort_obj_tgt"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(400), nullable=False)
    target_platform: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # snowflake | databricks | fabric
    person_days: Mapped[float] = mapped_column(Float, nullable=False)
    breakdown: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)


class Wave(Base, AuditMixin):
    __tablename__ = "wave"
    __table_args__ = (UniqueConstraint("project_id", "wave_number", name="uq_wave_num"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    wave_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="planned", nullable=False)

    members: Mapped[list[WaveMember]] = relationship(
        back_populates="wave", cascade="all, delete-orphan"
    )


class WaveMember(Base, AuditMixin):
    __tablename__ = "wave_member"
    __table_args__ = (UniqueConstraint("wave_id", "object_key", name="uq_wave_member_obj"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    wave_id: Mapped[str] = mapped_column(ForeignKey("wave.id", ondelete="CASCADE"), nullable=False)
    object_key: Mapped[str] = mapped_column(String(400), nullable=False)
    object_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    topo_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    wave: Mapped[Wave] = relationship(back_populates="members")


# ---------------------------------------------------------------------------
# 6. Conversion: conversion_run, generated_artifact, residue_item, review_decision
# ---------------------------------------------------------------------------


class ConversionRun(Base, AuditMixin):
    __tablename__ = "conversion_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    wave_id: Mapped[str | None] = mapped_column(ForeignKey("wave.id", ondelete="SET NULL"))
    config_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("config_version.id", ondelete="SET NULL")
    )
    target_platform: Mapped[str] = mapped_column(String(50), nullable=False)
    output_mode: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="running", nullable=False)
    git_branch: Mapped[str | None] = mapped_column(String(200))
    git_commit_sha: Mapped[str | None] = mapped_column(String(64))
    pr_reference: Mapped[str | None] = mapped_column(String(500))
    summary: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GeneratedArtifact(Base, AuditMixin):
    __tablename__ = "generated_artifact"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "object_key", "relative_path", "target_platform", name="uq_gen_art"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    conversion_run_id: Mapped[str] = mapped_column(
        ForeignKey("conversion_run.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(400), nullable=False)
    target_platform: Mapped[str] = mapped_column(String(50), nullable=False)
    artifact_kind: Mapped[str] = mapped_column(
        String(40), nullable=False
    )  # ddl | sql | dbt_model | dbt_schema | pyspark | task | pipeline_json
    relative_path: Mapped[str] = mapped_column(String(400), nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    compile_status: Mapped[str] = mapped_column(
        String(30), default="pending", nullable=False
    )  # passed | failed | warning
    compile_log: Mapped[str] = mapped_column(Text, default="", nullable=False)


class ResidueItem(Base, AuditMixin):
    __tablename__ = "residue_item"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    conversion_run_id: Mapped[str] = mapped_column(
        ForeignKey("conversion_run.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(400), nullable=False)
    # Stable component ID from the neutral model (node or task id), used to re-apply approved fixes
    component_key: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    component_name: Mapped[str] = mapped_column(String(255), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    reason_detail: Mapped[str] = mapped_column(Text, default="", nullable=False)
    original_snippet: Mapped[str] = mapped_column(Text, nullable=False)
    ai_proposed_code: Mapped[str | None] = mapped_column(Text)
    ai_rationale: Mapped[str | None] = mapped_column(Text)
    ai_model: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(
        String(30), default="open", nullable=False
    )  # open | ai_proposed | accepted | rejected | resolved


class ReviewDecision(Base, AuditMixin):
    __tablename__ = "review_decision"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(400), nullable=False)
    residue_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("residue_item.id", ondelete="SET NULL")
    )
    component_key: Mapped[str | None] = mapped_column(String(255))
    decision: Mapped[str] = mapped_column(String(30), nullable=False)  # approved | rejected
    replacement_code: Mapped[str | None] = mapped_column(Text)
    reviewer: Mapped[str] = mapped_column(String(120), nullable=False)
    comment: Mapped[str] = mapped_column(Text, default="", nullable=False)


# ---------------------------------------------------------------------------
# 7. Movement: load_run, load_partition
# ---------------------------------------------------------------------------


class LoadRun(Base, AuditMixin):
    __tablename__ = "load_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(400), nullable=False)
    mover_plugin: Mapped[str] = mapped_column(String(80), nullable=False)
    # Fully qualified table actually written on the target (after naming rules)
    target_table: Mapped[str | None] = mapped_column(String(400))
    mode: Mapped[str] = mapped_column(
        String(30), default="bulk", nullable=False
    )  # bulk | incremental
    watermark_value: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(30), default="running", nullable=False)
    rows_loaded: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    config_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("config_version.id", ondelete="SET NULL")
    )
    evidence_uri: Mapped[str | None] = mapped_column(String(500))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    partitions: Mapped[list[LoadPartition]] = relationship(
        back_populates="load_run", cascade="all, delete-orphan"
    )


class LoadPartition(Base, AuditMixin):
    __tablename__ = "load_partition"
    __table_args__ = (UniqueConstraint("load_run_id", "partition_index", name="uq_load_part_idx"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    load_run_id: Mapped[str] = mapped_column(
        ForeignKey("load_run.id", ondelete="CASCADE"), nullable=False
    )
    partition_index: Mapped[int] = mapped_column(Integer, nullable=False)
    range_predicate: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    parquet_uri: Mapped[str | None] = mapped_column(String(500))
    rows_extracted: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    rows_loaded: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), default="pending", nullable=False
    )  # pending | running | completed | failed
    error_message: Mapped[str | None] = mapped_column(Text)

    load_run: Mapped[LoadRun] = relationship(back_populates="partitions")


# ---------------------------------------------------------------------------
# 8. Reconciliation: recon_rule, recon_run, recon_result
# ---------------------------------------------------------------------------


class ReconRule(Base, AuditMixin):
    __tablename__ = "recon_rule"
    __table_args__ = (UniqueConstraint("project_id", "object_key", name="uq_recon_rule_obj"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(400), nullable=False)
    level: Mapped[str] = mapped_column(
        String(10), default="L2", nullable=False
    )  # L1 | L2 | L3 | L4 | JOB
    numeric_tolerance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    partition_key: Mapped[str | None] = mapped_column(String(120))
    masked_columns: Mapped[list[str]] = mapped_column(JsonType, default=list, nullable=False)


class ReconRun(Base, AuditMixin):
    __tablename__ = "recon_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str | None] = mapped_column(String(400))
    wave_id: Mapped[str | None] = mapped_column(ForeignKey("wave.id", ondelete="SET NULL"))
    config_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("config_version.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(30), default="running", nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    results: Mapped[list[ReconResult]] = relationship(
        back_populates="recon_run", cascade="all, delete-orphan"
    )


class ReconResult(Base, AuditMixin):
    __tablename__ = "recon_result"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    recon_run_id: Mapped[str] = mapped_column(
        ForeignKey("recon_run.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(400), nullable=False)
    level: Mapped[str] = mapped_column(String(10), nullable=False)
    verdict: Mapped[str] = mapped_column(
        String(30), nullable=False
    )  # pass | pass_within_tolerance | fail
    source_metrics: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    target_metrics: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    diff_summary: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    evidence_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    # Set for job-level checks: the converted code that was run against the legacy logic
    generated_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("generated_artifact.id", ondelete="SET NULL")
    )
    retention_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    recon_run: Mapped[ReconRun] = relationship(back_populates="results")


# ---------------------------------------------------------------------------
# 9. Tracking: object_status, status_event, signoff
# ---------------------------------------------------------------------------


class PipelineRun(Base, AuditMixin):
    """One click of Run: the stages run in the background and report progress here."""

    __tablename__ = "pipeline_run"
    __table_args__ = (Index("ix_pipeline_run_project", "project_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    # queued | running | succeeded | failed | interrupted
    current_stage: Mapped[str | None] = mapped_column(String(30))
    # [{"name", "status", "started_at", "finished_at", "summary", "error"}]
    stages: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, default=list, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ObjectStatus(Base, AuditMixin):
    __tablename__ = "object_status"
    __table_args__ = (UniqueConstraint("project_id", "object_key", name="uq_obj_status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(400), nullable=False)
    object_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    owner: Mapped[str] = mapped_column(String(120), default="unassigned", nullable=False)
    state: Mapped[str] = mapped_column(String(40), default="Discovered", nullable=False)
    blocker_reason: Mapped[str | None] = mapped_column(Text)
    # Content hash of the source the current state was reached on; a change sends the object back
    source_hash: Mapped[str | None] = mapped_column(String(64))
    last_evidence_ref: Mapped[str | None] = mapped_column(String(500))


class StatusEvent(Base, AuditMixin):
    __tablename__ = "status_event"
    __table_args__ = (Index("ix_status_event_obj", "project_id", "object_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(400), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(40))
    to_state: Mapped[str] = mapped_column(String(40), nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(60), nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(120), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)


class Signoff(Base, AuditMixin):
    __tablename__ = "signoff"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(400), nullable=False)
    recon_run_id: Mapped[str] = mapped_column(
        ForeignKey("recon_run.id", ondelete="RESTRICT"), nullable=False
    )
    approver: Mapped[str] = mapped_column(String(120), nullable=False)
    decision: Mapped[str] = mapped_column(String(30), default="signed_off", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    evidence_bundle_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    signed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# 10. Platform: user, role, project_member, audit_log, plugin_registry
# ---------------------------------------------------------------------------


class Role(Base, AuditMixin):
    __tablename__ = "role"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    permissions: Mapped[list[str]] = mapped_column(JsonType, default=list, nullable=False)


class User(Base, AuditMixin):
    __tablename__ = "app_user"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    username: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    role_name: Mapped[str] = mapped_column(String(50), default="viewer", nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ProjectMember(Base, AuditMixin):
    """Which user can see and act on which client's project, and in which role."""

    __tablename__ = "project_member"
    __table_args__ = (UniqueConstraint("project_id", "user_id", name="uq_project_member"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    role_name: Mapped[str] = mapped_column(String(50), nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_project_ts", "project_id", "timestamp"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    project_id: Mapped[str | None] = mapped_column(String(36))
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(200), nullable=False)
    config_version_id: Mapped[str | None] = mapped_column(String(36))
    details: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)


class PluginRegistryEntry(Base, AuditMixin):
    __tablename__ = "plugin_registry"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    plugin_type: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_golden_status: Mapped[str] = mapped_column(String(30), default="untested", nullable=False)
