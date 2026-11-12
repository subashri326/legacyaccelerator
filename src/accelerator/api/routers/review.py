"""Residue review queue, opt-in AI assist, human accept/reject, and deploy routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from accelerator.api.deps import CurrentUser, get_db, require_project_perm
from accelerator.api.schemas import DeployRequest, ResidueDecisionRequest
from accelerator.core.security import Permission
from accelerator.db.models import ResidueItem
from accelerator.engines.convert import ConvertEngine

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["review"])


@router.get("/residues")
def list_residues(
    project_id: str,
    _user: CurrentUser = Depends(require_project_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    items = db.query(ResidueItem).filter(ResidueItem.project_id == project_id).all()
    return [
        {
            "id": r.id,
            "object_key": r.object_key,
            "component_name": r.component_name,
            "reason_code": r.reason_code,
            "reason_detail": r.reason_detail,
            "original_snippet": r.original_snippet,
            "ai_proposed_code": r.ai_proposed_code,
            "ai_rationale": r.ai_rationale,
            "ai_model": r.ai_model,
            "status": r.status,
        }
        for r in items
    ]


@router.post("/residues/{residue_id}/ai-propose")
def ai_propose_residue(
    project_id: str,
    residue_id: str,
    user: CurrentUser = Depends(require_project_perm(Permission.REVIEW_RESIDUE)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    item = ConvertEngine().propose_ai_fix(
        db, project_id, residue_id, actor=user.username, actor_role=user.role
    )
    db.commit()
    return {
        "id": item.id,
        "status": item.status,
        "ai_proposed_code": item.ai_proposed_code,
        "ai_rationale": item.ai_rationale,
        "ai_model": item.ai_model,
    }


@router.post("/residues/{residue_id}/decide")
def decide_residue(
    project_id: str,
    residue_id: str,
    body: ResidueDecisionRequest,
    user: CurrentUser = Depends(require_project_perm(Permission.REVIEW_RESIDUE)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    item = ConvertEngine().decide_residue(
        db,
        project_id,
        residue_id,
        decision=body.decision,
        replacement_code=body.replacement_code,
        comment=body.comment,
        actor=user.username,
        actor_role=user.role,
    )
    db.commit()
    return {"id": item.id, "object_key": item.object_key, "status": item.status}


@router.post("/objects/{object_key:path}/deploy")
def deploy_object(
    project_id: str,
    object_key: str,
    body: DeployRequest = DeployRequest(),
    user: CurrentUser = Depends(require_project_perm(Permission.DEPLOY_CODE)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    status = ConvertEngine().approve_and_deploy_object(
        db,
        project_id,
        object_key,
        actor=user.username,
        actor_role=user.role,
        comment=body.comment,
    )
    db.commit()
    return {"object_key": status.object_key, "state": status.state}
