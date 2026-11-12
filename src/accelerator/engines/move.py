"""Stage 4: Move Data Engine — partitioned Parquet loads, incremental watermarks & checkpoints."""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from accelerator.core.observability import metrics
from accelerator.core.storage import ArtifactStore, get_artifact_store
from accelerator.db.base import utcnow
from accelerator.db.models import (
    DbColumn,
    DbObject,
    GeneratedArtifact,
    LoadPartition,
    LoadRun,
    WaveMember,
)
from accelerator.db.repository import get_active_config, record_audit
from accelerator.engines.state_machine import ObjectState, StateMachine
from accelerator.ir.models import ColumnIR, TableIR
from accelerator.plugins.registry import PluginRegistry, get_plugin_registry


@dataclass
class MoveSummary:
    project_id: str
    tables_loaded: int
    total_rows_loaded: int
    load_run_ids: list[str]


class MoveEngine:
    """Executes idempotent, partition-checkpointed bulk and incremental table loads."""

    def __init__(
        self,
        registry: PluginRegistry | None = None,
        store: ArtifactStore | None = None,
    ) -> None:
        self.registry = registry or get_plugin_registry()
        self.store = store or get_artifact_store()

    def run(
        self,
        db: Session,
        project_id: str,
        *,
        object_key: str | None = None,
        wave_id: str | None = None,
        mode: str = "bulk",
        resume_load_run_id: str | None = None,
        actor: str = "system",
        actor_role: str = "engineer",
    ) -> MoveSummary:
        with metrics.stage_span("move", project_id):
            cfg_ver, cfg = get_active_config(db, project_id)
            cfg.require_target()
            mover = self.registry.get_mover(cfg.movement.mover)

            # Determine tables in scope
            q = db.query(DbObject).filter(
                DbObject.project_id == project_id,
                DbObject.object_type == "TABLE",
            )
            if object_key:
                q = q.filter(DbObject.fqn == object_key)
            elif wave_id:
                members = db.query(WaveMember).filter(WaveMember.wave_id == wave_id).all()
                keys = [m.object_key for m in members]
                q = q.filter(DbObject.fqn.in_(keys))

            tables = q.all()
            total_rows = 0
            run_ids: list[str] = []

            for obj in tables:
                cols = (
                    db.query(DbColumn)
                    .filter(DbColumn.db_object_id == obj.id)
                    .order_by(DbColumn.ordinal_position)
                    .all()
                )
                tbl_ir = TableIR(
                    database=obj.database_name,
                    schema_name=obj.schema_name,
                    name=obj.object_name,
                    columns=[
                        ColumnIR(
                            name=c.column_name,
                            data_type=c.data_type,
                            nullable=c.nullable,
                            is_primary_key=c.is_primary_key,
                            ordinal_position=c.ordinal_position,
                        )
                        for c in cols
                    ],
                    row_count=obj.row_count,
                )

                # Support resuming from failed/partial partition checkpoints
                completed_parts: set[int] = set()
                prev_watermark: str | None = None
                if resume_load_run_id:
                    prev_run = (
                        db.query(LoadRun)
                        .filter(LoadRun.id == resume_load_run_id, LoadRun.object_key == obj.fqn)
                        .first()
                    )
                    if prev_run:
                        completed_parts = {
                            p.partition_index
                            for p in prev_run.partitions
                            if p.status == "completed"
                        }
                        prev_watermark = prev_run.watermark_value
                elif mode == "incremental":
                    last_run = (
                        db.query(LoadRun)
                        .filter(
                            LoadRun.project_id == project_id,
                            LoadRun.object_key == obj.fqn,
                            LoadRun.status == "completed",
                        )
                        .order_by(LoadRun.created_at.desc())
                        .first()
                    )
                    if last_run:
                        prev_watermark = last_run.watermark_value

                ddl_art = (
                    db.query(GeneratedArtifact)
                    .filter(
                        GeneratedArtifact.project_id == project_id,
                        GeneratedArtifact.object_key == obj.fqn,
                        GeneratedArtifact.artifact_kind == "ddl",
                    )
                    .first()
                )
                ddl_sql = self.store.get_text(ddl_art.storage_uri) if ddl_art else None

                load_run = LoadRun(
                    project_id=project_id,
                    object_key=obj.fqn,
                    mover_plugin=mover.manifest.name,
                    mode=mode,
                    watermark_value=prev_watermark,
                    status="running",
                    config_version_id=cfg_ver.id,
                    created_by=actor,
                )
                db.add(load_run)
                db.flush()

                def _stage_partition(
                    p_idx: int, pq_bytes: bytes, _fqn: str = obj.fqn, _rid: str = load_run.id
                ) -> str:
                    safe = _fqn.replace(".", "_")
                    key = f"projects/{project_id}/{cfg.movement.staging_prefix}/{safe}/{_rid}/part_{p_idx:04d}.parquet"
                    return self.store.put_bytes(key, pq_bytes)

                outcome = mover.load_table(
                    table=tbl_ir,
                    source_uri=cfg.source.connection,
                    target_uri=cfg.target.connection,
                    config=cfg,
                    mode=mode,
                    watermark_value=prev_watermark,
                    existing_completed_partitions=completed_parts,
                    staging_writer=_stage_partition,
                    target_ddl_sql=ddl_sql,
                )

                for part in outcome.partitions:
                    db.add(
                        LoadPartition(
                            load_run_id=load_run.id,
                            partition_index=part.partition_index,
                            range_predicate=part.range_predicate,
                            parquet_uri=part.parquet_uri,
                            rows_extracted=part.rows_extracted,
                            rows_loaded=part.rows_loaded,
                            checksum=part.checksum,
                            status=part.status,
                            error_message=part.error_message,
                            created_by=actor,
                        )
                    )

                evidence_uri = self.store.put_text(
                    f"projects/{project_id}/evidence/move/{load_run.id}.json",
                    json.dumps(
                        {
                            "load_run_id": load_run.id,
                            "object_key": obj.fqn,
                            "mode": mode,
                            "rows_loaded": outcome.rows_loaded,
                            "watermark_value": outcome.watermark_value,
                            "evidence": outcome.evidence,
                        },
                        indent=2,
                    ),
                )
                load_run.rows_loaded = outcome.rows_loaded
                load_run.watermark_value = outcome.watermark_value
                load_run.status = "completed"
                load_run.evidence_uri = evidence_uri
                load_run.completed_at = utcnow()
                db.flush()

                total_rows += outcome.rows_loaded
                run_ids.append(load_run.id)

                StateMachine.advance(
                    db,
                    project_id,
                    obj.fqn,
                    "table",
                    ObjectState.LOADED,
                    actor,
                    "load_run",
                    load_run.id,
                )

            record_audit(
                db,
                actor=actor,
                actor_role=actor_role,
                action="stage.move.completed",
                resource_type="project",
                resource_id=project_id,
                project_id=project_id,
                config_version_id=cfg_ver.id,
                details={"tables_loaded": len(run_ids), "total_rows": total_rows, "mode": mode},
            )

            return MoveSummary(
                project_id=project_id,
                tables_loaded=len(run_ids),
                total_rows_loaded=total_rows,
                load_run_ids=run_ids,
            )
