"""Tracking dashboard, reconciliation evidence, load checkpoints, audit log, sign-off, and exports."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from accelerator.api.deps import CurrentUser, get_db, require_project_perm
from accelerator.api.schemas import SignoffRequest
from accelerator.core.security import Permission
from accelerator.core.storage import get_artifact_store
from accelerator.db.models import AuditLog, LoadRun, ReconResult, Signoff, StatusEvent
from accelerator.engines.track import TrackEngine

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["track"])


@router.get("/dashboard")
def dashboard(
    project_id: str,
    _user: CurrentUser = Depends(require_project_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return TrackEngine().get_dashboard(db, project_id)


@router.get("/reconciliation")
def list_reconciliation(
    project_id: str,
    _user: CurrentUser = Depends(require_project_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    rows = (
        db.query(ReconResult)
        .filter(ReconResult.project_id == project_id)
        .order_by(ReconResult.created_at.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "recon_run_id": r.recon_run_id,
            "object_key": r.object_key,
            "level": r.level,
            "verdict": r.verdict,
            "source_metrics": r.source_metrics,
            "target_metrics": r.target_metrics,
            "diff_summary": r.diff_summary,
            "evidence_uri": r.evidence_uri,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/load-runs")
def list_load_runs(
    project_id: str,
    _user: CurrentUser = Depends(require_project_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    runs = (
        db.query(LoadRun)
        .filter(LoadRun.project_id == project_id)
        .order_by(LoadRun.created_at.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "object_key": r.object_key,
            "mover_plugin": r.mover_plugin,
            "mode": r.mode,
            "watermark_value": r.watermark_value,
            "status": r.status,
            "rows_loaded": r.rows_loaded,
            "partitions": [
                {
                    "index": p.partition_index,
                    "status": p.status,
                    "rows_loaded": p.rows_loaded,
                    "checksum": p.checksum,
                    "parquet_uri": p.parquet_uri,
                }
                for p in r.partitions
            ],
        }
        for r in runs
    ]


@router.get("/signoffs")
def list_signoffs(
    project_id: str,
    _user: CurrentUser = Depends(require_project_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    rows = db.query(Signoff).filter(Signoff.project_id == project_id).all()
    return [
        {
            "id": s.id,
            "object_key": s.object_key,
            "approver": s.approver,
            "decision": s.decision,
            "notes": s.notes,
            "recon_run_id": s.recon_run_id,
            "evidence_bundle_uri": s.evidence_bundle_uri,
            "signed_at": s.signed_at.isoformat(),
        }
        for s in rows
    ]


@router.post("/signoff")
def sign_off(
    project_id: str,
    body: SignoffRequest,
    user: CurrentUser = Depends(require_project_perm(Permission.SIGN_OFF)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    row = TrackEngine().sign_off(
        db,
        project_id,
        object_key=body.object_key,
        approver=user.username,
        approver_role=user.role,
        notes=body.notes,
        recon_run_id=body.recon_run_id,
    )
    db.commit()
    return {
        "signoff_id": row.id,
        "object_key": row.object_key,
        "approver": row.approver,
        "evidence_bundle_uri": row.evidence_bundle_uri,
    }


@router.get("/audit-log")
def get_audit_log(
    project_id: str,
    _user: CurrentUser = Depends(require_project_perm(Permission.VIEW_AUDIT)),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.project_id == project_id)
        .order_by(AuditLog.timestamp.desc())
        .limit(200)
        .all()
    )
    return [
        {
            "id": a.id,
            "timestamp": a.timestamp.isoformat(),
            "actor": a.actor,
            "actor_role": a.actor_role,
            "action": a.action,
            "resource_type": a.resource_type,
            "resource_id": a.resource_id,
            "details": a.details,
        }
        for a in rows
    ]


@router.get("/objects/{object_key:path}/history")
def object_history(
    project_id: str,
    object_key: str,
    _user: CurrentUser = Depends(require_project_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    rows = (
        db.query(StatusEvent)
        .filter(StatusEvent.project_id == project_id, StatusEvent.object_key == object_key)
        .order_by(StatusEvent.created_at)
        .all()
    )
    return [
        {
            "from_state": e.from_state,
            "to_state": e.to_state,
            "actor": e.actor,
            "evidence_type": e.evidence_type,
            "evidence_id": e.evidence_id,
            "timestamp": e.created_at.isoformat(),
        }
        for e in rows
    ]


@router.get("/exports/steering")
def export_steering(
    project_id: str,
    format: str = "markdown",
    _user: CurrentUser = Depends(require_project_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> Response:
    uris = TrackEngine().export_steering_summary(db, project_id)
    store = get_artifact_store()
    if format == "excel":
        data = store.get_bytes(uris["excel_uri"])
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=steering_summary.xlsx"},
        )
    md = store.get_text(uris["markdown_uri"])
    return Response(content=md, media_type="text/markdown")
