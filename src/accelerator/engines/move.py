"""Stage 4: Move Data Engine — partitioned Parquet loads, incremental watermarks & checkpoints."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from accelerator.core.observability import metrics
from accelerator.core.storage import ArtifactStore, get_artifact_store
from accelerator.db.base import utcnow
from accelerator.db.models import (
    DbObject,
    GeneratedArtifact,
    LoadPartition,
    LoadRun,
    ObjectStatus,
    WaveMember,
)
from accelerator.db.repository import get_active_config, record_audit, table_ir_from_db
from accelerator.engines.state_machine import STATE_ORDER, ObjectState, StateMachine
from accelerator.plugins.interfaces import PartitionResult
from accelerator.plugins.registry import PluginRegistry, get_plugin_registry

logger = logging.getLogger(__name__)


@dataclass
class MoveSummary:
    project_id: str
    tables_loaded: int
    total_rows_loaded: int
    load_run_ids: list[str]
    failed_tables: list[str] = field(default_factory=list)
    # Tables not moved because their conversion is not finished (open review items).
    held_tables: list[str] = field(default_factory=list)


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
            failed: list[str] = []
            run_ids: list[str] = []

            held: list[str] = []
            for obj in tables:
                status = (
                    db.query(ObjectStatus)
                    .filter(
                        ObjectStatus.project_id == project_id, ObjectStatus.object_key == obj.fqn
                    )
                    .first()
                )
                if (
                    status is not None
                    and STATE_ORDER.get(ObjectState(status.state), 0)
                    < STATE_ORDER[ObjectState.CONVERTED]
                ):
                    held.append(obj.fqn)  # e.g. a type mapping still awaiting approval
                    continue
                tbl_ir = table_ir_from_db(db, obj)

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
                target_tbl = self.registry.get_generator(cfg.require_target()).target_table(
                    tbl_ir, cfg
                )

                # Resume continues the same run after its last checkpoint; otherwise a new run.
                prev_run = None
                if resume_load_run_id:
                    prev_run = (
                        db.query(LoadRun)
                        .filter(LoadRun.id == resume_load_run_id, LoadRun.object_key == obj.fqn)
                        .first()
                    )
                prev_watermark: str | None = None
                if mode == "incremental":
                    last_ok = (
                        db.query(LoadRun)
                        .filter(
                            LoadRun.project_id == project_id,
                            LoadRun.object_key == obj.fqn,
                            LoadRun.status == "completed",
                        )
                        .order_by(LoadRun.created_at.desc())
                        .first()
                    )
                    prev_watermark = last_ok.watermark_value if last_ok else None

                if prev_run is not None:
                    load_run = prev_run
                    load_run.status = "running"
                    start_idx = len(load_run.partitions)
                else:
                    load_run = LoadRun(
                        project_id=project_id,
                        object_key=obj.fqn,
                        mover_plugin=mover.manifest.name,
                        target_table=f"{target_tbl.schema_name}.{target_tbl.name}",
                        mode=mode,
                        watermark_value=prev_watermark,
                        status="running",
                        config_version_id=cfg_ver.id,
                        created_by=actor,
                    )
                    db.add(load_run)
                    start_idx = 0
                db.commit()  # the run is visible (and resumable) before any data moves

                def _stage_partition(
                    p_idx: int, pq_bytes: bytes, _fqn: str = obj.fqn, _rid: str = load_run.id
                ) -> str:
                    safe = _fqn.replace(".", "_")
                    key = f"projects/{project_id}/{cfg.movement.staging_prefix}/{safe}/{_rid}/part_{p_idx:04d}.parquet"
                    return self.store.put_bytes(key, pq_bytes)

                def _checkpoint(part: PartitionResult, _run: LoadRun = load_run) -> None:
                    # Committed straight away: a crash later keeps every finished chunk.
                    db.add(
                        LoadPartition(
                            load_run_id=_run.id,
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
                    _run.rows_loaded = (_run.rows_loaded or 0) + part.rows_loaded
                    db.commit()

                try:
                    outcome = mover.load_table(
                        table=tbl_ir,
                        source_uri=cfg.source.connection,
                        target_uri=cfg.target.connection,
                        config=cfg,
                        mode=mode,
                        watermark_value=load_run.watermark_value if mode == "incremental" else None,
                        resume=prev_run is not None,
                        start_partition_index=start_idx,
                        staging_writer=_stage_partition,
                        target_ddl_sql=ddl_sql,
                        target_table=target_tbl,
                        on_partition=_checkpoint,
                    )
                except Exception as exc:
                    logger.error("Copying %s failed: %s", obj.fqn, exc)
                    db.rollback()
                    load_run.status = "failed"
                    load_run.evidence_uri = self.store.put_text(
                        f"projects/{project_id}/evidence/move/{load_run.id}_failed.json",
                        json.dumps(
                            {
                                "load_run_id": load_run.id,
                                "object_key": obj.fqn,
                                "error": f"{type(exc).__name__}: {exc}",
                            },
                            indent=2,
                        ),
                    )
                    StateMachine.set_blocker(
                        db,
                        project_id,
                        obj.fqn,
                        f"Load failed (resume with load run {load_run.id}): {exc}",
                    )
                    db.commit()
                    failed.append(obj.fqn)
                    run_ids.append(load_run.id)
                    continue

                evidence_uri = self.store.put_text(
                    f"projects/{project_id}/evidence/move/{load_run.id}.json",
                    json.dumps(
                        {
                            "load_run_id": load_run.id,
                            "object_key": obj.fqn,
                            "mode": mode,
                            "rows_loaded_this_attempt": outcome.rows_loaded,
                            "rows_loaded_total": load_run.rows_loaded,
                            "watermark_value": outcome.watermark_value,
                            "evidence": outcome.evidence,
                        },
                        indent=2,
                        default=str,
                    ),
                )
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
                db.commit()

            record_audit(
                db,
                actor=actor,
                actor_role=actor_role,
                action="stage.move.completed",
                resource_type="project",
                resource_id=project_id,
                project_id=project_id,
                config_version_id=cfg_ver.id,
                details={
                    "tables_loaded": len(run_ids) - len(failed),
                    "failed_tables": failed,
                    "total_rows": total_rows,
                    "mode": mode,
                },
            )

            return MoveSummary(
                project_id=project_id,
                tables_loaded=len(run_ids) - len(failed),
                total_rows_loaded=total_rows,
                load_run_ids=run_ids,
                failed_tables=failed,
                held_tables=held,
            )
