"""Inventory, Neutral IR, Lineage, Wave Plan, and Generated Artifacts inspection routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from accelerator.api.deps import CurrentUser, get_db, require_perm
from accelerator.core.security import Permission
from accelerator.core.storage import get_artifact_store
from accelerator.db.models import (
    ComplexityScore,
    DbObject,
    Dependency,
    EffortEstimate,
    EtlJob,
    GeneratedArtifact,
    IrFlow,
    ObjectStatus,
    Wave,
    WaveMember,
)

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["objects"])


@router.get("/inventory")
def get_inventory(
    project_id: str,
    _user: CurrentUser = Depends(require_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    objs = db.query(DbObject).filter(DbObject.project_id == project_id).all()
    jobs = db.query(EtlJob).filter(EtlJob.project_id == project_id).all()
    scores = {s.object_key: s for s in db.query(ComplexityScore).filter(ComplexityScore.project_id == project_id).all()}
    statuses = {s.object_key: s for s in db.query(ObjectStatus).filter(ObjectStatus.project_id == project_id).all()}
    efforts = db.query(EffortEstimate).filter(EffortEstimate.project_id == project_id).all()
    eff_by_key: dict[str, dict[str, float]] = {}
    for e in efforts:
        eff_by_key.setdefault(e.object_key, {})[e.target_platform] = e.person_days

    items = []
    for o in objs:
        sc = scores.get(o.fqn)
        st = statuses.get(o.fqn)
        items.append(
            {
                "object_key": o.fqn,
                "name": o.object_name,
                "kind": o.object_type.lower(),
                "row_count": o.row_count,
                "is_dead_candidate": o.is_dead_candidate,
                "state": st.state if st else "Discovered",
                "blocker_reason": st.blocker_reason if st else None,
                "complexity_score": sc.score if sc else None,
                "classification": sc.classification if sc else None,
                "reasons": sc.reasons if sc else [],
                "effort_days": eff_by_key.get(o.fqn, {}),
            }
        )
    for j in jobs:
        sc = scores.get(j.job_name)
        st = statuses.get(j.job_name)
        items.append(
            {
                "object_key": j.job_name,
                "name": j.job_name,
                "kind": "etl_job",
                "source_tool": j.source_tool,
                "state": st.state if st else "Discovered",
                "blocker_reason": st.blocker_reason if st else None,
                "complexity_score": sc.score if sc else None,
                "classification": sc.classification if sc else None,
                "reasons": sc.reasons if sc else [],
                "effort_days": eff_by_key.get(j.job_name, {}),
            }
        )
    return {"items": items}


@router.get("/lineage")
def get_lineage(
    project_id: str,
    _user: CurrentUser = Depends(require_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    deps = db.query(Dependency).filter(Dependency.project_id == project_id).all()
    nodes = sorted({d.upstream_key for d in deps} | {d.downstream_key for d in deps})
    return {
        "nodes": nodes,
        "edges": [
            {
                "upstream": d.upstream_key,
                "downstream": d.downstream_key,
                "edge_type": d.edge_type,
                "column_lineage": d.column_lineage,
            }
            for d in deps
        ],
    }


@router.get("/waves")
def get_waves(
    project_id: str,
    _user: CurrentUser = Depends(require_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    waves = db.query(Wave).filter(Wave.project_id == project_id).order_by(Wave.wave_number).all()
    out = []
    for w in waves:
        members = (
            db.query(WaveMember)
            .filter(WaveMember.wave_id == w.id)
            .order_by(WaveMember.topo_order)
            .all()
        )
        out.append(
            {
                "id": w.id,
                "wave_number": w.wave_number,
                "name": w.name,
                "status": w.status,
                "members": [
                    {"object_key": m.object_key, "object_kind": m.object_kind, "topo_order": m.topo_order}
                    for m in members
                ],
            }
        )
    return out


@router.get("/ir/{flow_key:path}")
def get_neutral_ir(
    project_id: str,
    flow_key: str,
    _user: CurrentUser = Depends(require_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    flow = db.query(IrFlow).filter(IrFlow.project_id == project_id, IrFlow.flow_key == flow_key).first()
    if flow is None:
        raise ValueError(f"IR flow '{flow_key}' not found")
    return {"flow_key": flow.flow_key, "source_tool": flow.source_tool, "ir": flow.ir_payload}


@router.get("/artifacts")
def list_artifacts(
    project_id: str,
    _user: CurrentUser = Depends(require_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    arts = db.query(GeneratedArtifact).filter(GeneratedArtifact.project_id == project_id).all()
    return [
        {
            "id": a.id,
            "object_key": a.object_key,
            "target_platform": a.target_platform,
            "artifact_kind": a.artifact_kind,
            "relative_path": a.relative_path,
            "compile_status": a.compile_status,
            "compile_log": a.compile_log,
        }
        for a in arts
    ]


@router.get("/artifacts/{artifact_id}/content")
def get_artifact_content(
    project_id: str,
    artifact_id: str,
    _user: CurrentUser = Depends(require_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    art = (
        db.query(GeneratedArtifact)
        .filter(GeneratedArtifact.id == artifact_id, GeneratedArtifact.project_id == project_id)
        .first()
    )
    if art is None:
        raise ValueError(f"Artifact '{artifact_id}' not found")
    return {
        "id": art.id,
        "relative_path": art.relative_path,
        "compile_status": art.compile_status,
        "content": get_artifact_store().get_text(art.storage_uri),
    }
