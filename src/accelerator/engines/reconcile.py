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
    DbObject,
    ObjectStatus,
    ReconResult,
    ReconRule,
    ReconRun,
    WaveMember,
)
from accelerator.db.repository import get_active_config, record_audit, table_ir_from_db
from accelerator.engines.state_machine import ObjectState, StateMachine
from accelerator.plugins.interfaces import ValidationOutcome
from accelerator.plugins.registry import PluginRegistry, get_plugin_registry


@dataclass
class ReconcileSummary:
    recon_run_id: str
    project_id: str
    tables_checked: int
    passed: int
    passed_within_tolerance: int
    failed: int
    skipped_not_loaded: int = 0


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
            skipped = 0
            states = {
                s.object_key: s.state
                for s in db.query(ObjectStatus).filter(ObjectStatus.project_id == project_id)
            }
            generator = self.registry.get_generator(cfg.require_target())
            retention_dt = utcnow() + timedelta(days=cfg.reconciliation.l4_retention_days)

            for obj in tables:
                # Only tables that have actually been loaded can be compared.
                if states.get(obj.fqn) not in (
                    ObjectState.LOADED.value,
                    ObjectState.VALIDATED.value,
                    ObjectState.SIGNED_OFF.value,
                ):
                    skipped += 1
                    continue
                tbl_ir = table_ir_from_db(db, obj)

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

                try:
                    outcome = validator.reconcile_table(
                        table=tbl_ir,
                        rule=rule,
                        source_uri=cfg.source.connection,
                        target_uri=cfg.target.connection,
                        config=cfg,
                        target_table=generator.target_table(tbl_ir, cfg),
                    )
                except Exception as exc:  # one bad table must not stop the whole run
                    outcome = ValidationOutcome(
                        object_key=obj.fqn,
                        level=rule.level,
                        verdict="fail",
                        diff_summary={"error": f"{type(exc).__name__}: {exc}"},
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
                    retention_expires_at=retention_dt
                    if "l4_diff" in outcome.diff_summary
                    else None,
                    created_by=actor,
                )
                db.add(res_row)
                db.flush()

                if outcome.verdict == "pass":
                    passed += 1
                    StateMachine.advance(
                        db,
                        project_id,
                        obj.fqn,
                        "table",
                        ObjectState.VALIDATED,
                        actor,
                        "recon_run",
                        recon_run.id,
                    )
                elif outcome.verdict == "pass_within_tolerance":
                    within_tol += 1
                    StateMachine.advance(
                        db,
                        project_id,
                        obj.fqn,
                        "table",
                        ObjectState.VALIDATED,
                        actor,
                        "recon_run",
                        recon_run.id,
                    )
                else:
                    failed += 1
                    StateMachine.set_blocker(
                        db,
                        project_id,
                        obj.fqn,
                        f"Reconciliation ({outcome.level}) failed: {list(outcome.diff_summary.keys())}",
                    )

            purge_expired_samples(db, self.store)
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
                tables_checked=len(tables) - skipped,
                passed=passed,
                passed_within_tolerance=within_tol,
                failed=failed,
                skipped_not_loaded=skipped,
            )


def purge_expired_samples(db: Session, store: ArtifactStore | None = None) -> int:
    """Delete masked row samples once their retention period ends (counts stay as evidence).

    Removes the samples from the database record and rewrites the evidence file without them.
    Runs at API start-up and after every reconciliation.
    """
    store = store or get_artifact_store()
    now = utcnow()
    expired = (
        db.query(ReconResult)
        .filter(
            ReconResult.retention_expires_at.isnot(None), ReconResult.retention_expires_at <= now
        )
        .all()
    )
    for res in expired:
        diff = dict(res.diff_summary or {})
        l4 = dict(diff.get("l4_diff") or {})
        for k in ("only_in_source_sample", "only_in_target_sample", "value_mismatch_sample"):
            l4.pop(k, None)
        l4["samples_purged_at"] = now.isoformat()
        diff["l4_diff"] = l4
        res.diff_summary = diff
        res.retention_expires_at = None
        try:
            payload = json.loads(store.get_text(res.evidence_uri))
            payload["diff_summary"] = diff
            key = res.evidence_uri.split("://", 1)[-1]
            if res.evidence_uri.startswith("s3://") or res.evidence_uri.startswith("abfs://"):
                key = key.split("/", 1)[1]
            store.put_text(key, json.dumps(payload, indent=2, default=str))
        except Exception:  # evidence file missing: the database copy is already purged
            pass
    db.flush()
    return len(expired)
