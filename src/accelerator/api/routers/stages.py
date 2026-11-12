"""Stage execution endpoints: Discover, Assess, Convert, Move, Reconcile, and Run Pipeline."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from accelerator.api.deps import CurrentUser, get_db, require_perm
from accelerator.api.schemas import (
    ConvertRequest,
    DiscoverRequest,
    MoveRequest,
    ReconcileRequest,
)
from accelerator.core.security import Permission
from accelerator.engines.assess import AssessEngine
from accelerator.engines.convert import ConvertEngine
from accelerator.engines.discover import DiscoverEngine
from accelerator.engines.move import MoveEngine
from accelerator.engines.reconcile import ReconcileEngine
from accelerator.orchestration.runner import RunOrchestrator

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["stages"])


@router.post("/discover")
def run_discover(
    project_id: str,
    body: DiscoverRequest = DiscoverRequest(),
    user: CurrentUser = Depends(require_perm(Permission.RUN_DISCOVER)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    summary = DiscoverEngine().run(
        db,
        project_id,
        actor=user.username,
        actor_role=user.role,
        extra_etl_paths=body.extra_etl_paths,
        catalog_bundle_override=body.catalog_bundle_override,
    )
    db.commit()
    return asdict(summary)


@router.post("/discover/upload")
async def upload_and_discover(
    project_id: str,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_perm(Permission.RUN_DISCOVER)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    import tempfile
    from pathlib import Path

    raw = await file.read()
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / (file.filename or "artifact.dtsx")
        target.write_bytes(raw)
        summary = DiscoverEngine().run(
            db,
            project_id,
            actor=user.username,
            actor_role=user.role,
            extra_etl_paths=[str(target)],
        )
        db.commit()
        return asdict(summary)


@router.post("/assess")
def run_assess(
    project_id: str,
    user: CurrentUser = Depends(require_perm(Permission.RUN_ASSESS)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    summary = AssessEngine().run(db, project_id, actor=user.username, actor_role=user.role)
    db.commit()
    return asdict(summary)


@router.post("/convert")
def run_convert(
    project_id: str,
    body: ConvertRequest = ConvertRequest(),
    user: CurrentUser = Depends(require_perm(Permission.RUN_CONVERT)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    summary = ConvertEngine().run(
        db,
        project_id,
        wave_id=body.wave_id,
        object_key=body.object_key,
        generator_name=body.generator_name,
        deliver_git=body.deliver_git,
        git_repo_path=body.git_repo_path,
        actor=user.username,
        actor_role=user.role,
    )
    db.commit()
    return asdict(summary)


@router.post("/move")
def run_move(
    project_id: str,
    body: MoveRequest = MoveRequest(),
    user: CurrentUser = Depends(require_perm(Permission.RUN_MOVE)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    summary = MoveEngine().run(
        db,
        project_id,
        object_key=body.object_key,
        wave_id=body.wave_id,
        mode=body.mode,
        resume_load_run_id=body.resume_load_run_id,
        actor=user.username,
        actor_role=user.role,
    )
    db.commit()
    return asdict(summary)


@router.post("/reconcile")
def run_reconcile(
    project_id: str,
    body: ReconcileRequest = ReconcileRequest(),
    user: CurrentUser = Depends(require_perm(Permission.RUN_RECONCILE)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    summary = ReconcileEngine().run(
        db,
        project_id,
        object_key=body.object_key,
        wave_id=body.wave_id,
        level_override=body.level_override,
        actor=user.username,
        actor_role=user.role,
    )
    db.commit()
    return asdict(summary)


@router.post("/run-pipeline")
def run_full_pipeline(
    project_id: str,
    deliver_git: bool = False,
    user: CurrentUser = Depends(require_perm(Permission.RUN_CONVERT)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    res = RunOrchestrator().run_pipeline(
        db, project_id, actor=user.username, actor_role=user.role, deliver_git=deliver_git
    )
    db.commit()
    return {
        "discover": asdict(res.discover),
        "assess": asdict(res.assess),
        "convert": asdict(res.convert),
        "move": asdict(res.move),
        "reconcile": asdict(res.reconcile),
    }
