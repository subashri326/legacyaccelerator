"""Stage 5: Reconcile Engine — pushed-down L1-L4 & job-level validation with evidence storage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session

from accelerator.core.observability import metrics
from accelerator.core.storage import ArtifactStore, get_artifact_store
from accelerator.db.base import utcnow
from accelerator.db.models import (
    DbColumn,
    DbObject,
    ReconResult,
    ReconRule,
    ReconRun,
    WaveMember,
)
from accelerator.db.repository import get_active_config, record_audit
from accelerator.engines.state_machine import ObjectState, StateMachine
from accelerator.ir.models import ColumnIR, TableIR
from accelerator.plugins.registry import PluginRegistry, get_plugin_registry


@dataclass
class ReconcileSummary:
    recon_run_id: str
    project_id: str
    tables_checked: int
    passed: int
    passed_within_tolerance: int
    failed: int


class ReconcileEngine:
    """Runs pushed-down L1-L4 checks and persists auditable evidence without copying raw data."""

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
        level_override: str | None = None,
        actor: str = "system",
        actor_role: str = "engineer",
    ) -> ReconcileSummary:
        with metrics.stage_span("reconcile", project_id):
            cfg_ver, cfg = get_active_config(db, project_id)
            validator = self.registry.get_validator(cfg.require_target())

            recon_run = ReconRun(
                project_id=project_id,
                object_key=object_key,
                wave_id=wave_id,
                config_version_id=cfg_ver.id,
                status="running",
                created_by=actor,
            )
            db.add(recon_run)
            db.flush()

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
            passed = 0
            within_tol = 0
            failed = 0
            retention_dt = utcnow() + timedelta(days=cfg.reconciliation.l4_retention_days)

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
                )

                rule = cfg.reconciliation.get_table_rule(obj.fqn)
                if level_override:
                    rule = rule.model_copy(update={"level": level_override})

                # Upsert ReconRule metadata record
                existing_rule = (
                    db.query(ReconRule)
                    .filter(ReconRule.project_id == project_id, ReconRule.object_key == obj.fqn)
                    .first()
                )
                if existing_rule is None:
                    db.add(
                        ReconRule(
                            project_id=project_id,
                            object_key=obj.fqn,
                            level=rule.level,
                            numeric_tolerance=rule.numeric_tolerance,
                            partition_key=rule.partition_key,
                            masked_columns=rule.masked_columns,
                            created_by=actor,
                        )
                    )
                else:
                    existing_rule.level = rule.level
                    existing_rule.numeric_tolerance = rule.numeric_tolerance
                    existing_rule.masked_columns = rule.masked_columns

                outcome = validator.reconcile_table(
                    table=tbl_ir,
                    rule=rule,
                    source_uri=cfg.source.connection,
                    target_uri=cfg.target.connection,
                    config=cfg,
                )

                evidence_payload = {
                    "recon_run_id": recon_run.id,
                    "object_key": obj.fqn,
                    "level": outcome.level,
                    "verdict": outcome.verdict,
                    "source_metrics": outcome.source_metrics,
                    "target_metrics": outcome.target_metrics,
                    "diff_summary": outcome.diff_summary,
                }
                evidence_uri = self.store.put_text(
                    f"projects/{project_id}/evidence/reconcile/{recon_run.id}_{obj.object_name}.json",
                    json.dumps(evidence_payload, indent=2),
                )

                res_row = ReconResult(
                    recon_run_id=recon_run.id,
                    project_id=project_id,
                    object_key=obj.fqn,
                    level=outcome.level,
                    verdict=outcome.verdict,
                    source_metrics=outcome.source_metrics,
                    target_metrics=outcome.target_metrics,
                    diff_summary=outcome.diff_summary,
                    evidence_uri=evidence_uri,
                    retention_expires_at=retention_dt if "l4_diff" in outcome.diff_summary else None,
                    created_by=actor,
                )
                db.add(res_row)
                db.flush()

                if outcome.verdict == "pass":
                    passed += 1
                    StateMachine.advance(
                        db, project_id, obj.fqn, "table", ObjectState.VALIDATED, actor, "recon_run", recon_run.id
                    )
                elif outcome.verdict == "pass_within_tolerance":
                    within_tol += 1
                    StateMachine.advance(
                        db, project_id, obj.fqn, "table", ObjectState.VALIDATED, actor, "recon_run", recon_run.id
                    )
                else:
                    failed += 1
                    StateMachine.set_blocker(
                        db,
                        project_id,
                        obj.fqn,
                        f"Reconciliation ({outcome.level}) failed: {list(outcome.diff_summary.keys())}",
                    )

            recon_run.status = "completed"
            recon_run.completed_at = utcnow()
            db.flush()

            record_audit(
                db,
                actor=actor,
                actor_role=actor_role,
                action="stage.reconcile.completed",
                resource_type="recon_run",
                resource_id=recon_run.id,
                project_id=project_id,
                config_version_id=cfg_ver.id,
                details={"passed": passed, "within_tolerance": within_tol, "failed": failed},
            )

            return ReconcileSummary(
                recon_run_id=recon_run.id,
                project_id=project_id,
                tables_checked=len(tables),
                passed=passed,
                passed_within_tolerance=within_tol,
                failed=failed,
            )
