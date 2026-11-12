"""Projects, clients, config versioning, and secrets registration routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from accelerator.api.deps import CurrentUser, get_db, require_perm
from accelerator.api.schemas import RegisterProjectYamlRequest, SecretSetRequest
from accelerator.core.secrets import get_vault
from accelerator.core.security import Permission
from accelerator.db.models import Client, ConfigVersion, Project
from accelerator.db.repository import ensure_project_from_config

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


@router.get("")
def list_projects(
    _user: CurrentUser = Depends(require_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    projects = db.query(Project).order_by(Project.created_at.desc()).all()
    clients = {c.id: c for c in db.query(Client).all()}
    return [
        {
            "id": p.id,
            "slug": p.slug,
            "name": p.name,
            "client_slug": clients[p.client_id].slug if p.client_id in clients else "",
            "source_platform": p.source_platform,
            "etl_tool": p.etl_tool,
            "target_platform": p.target_platform,
            "status": p.status,
        }
        for p in projects
    ]


@router.post("/register-yaml")
def register_project_yaml(
    body: RegisterProjectYamlRequest,
    user: CurrentUser = Depends(require_perm(Permission.MANAGE_CONFIG)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    project, cfg_ver, cfg = ensure_project_from_config(
        db, body.raw_yaml, actor=user.username, actor_role=user.role
    )
    db.commit()
    return {
        "project_id": project.id,
        "project_slug": project.slug,
        "client": cfg.client,
        "config_version_id": cfg_ver.id,
        "version_num": cfg_ver.version_num,
        "config_hash": cfg_ver.config_hash,
    }


@router.get("/{project_id}")
def get_project(
    project_id: str,
    _user: CurrentUser = Depends(require_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    p = db.query(Project).filter(Project.id == project_id).first()
    if p is None:
        raise ValueError(f"Project '{project_id}' not found")
    cfgs = (
        db.query(ConfigVersion)
        .filter(ConfigVersion.project_id == project_id)
        .order_by(ConfigVersion.version_num.desc())
        .all()
    )
    return {
        "id": p.id,
        "slug": p.slug,
        "name": p.name,
        "source_platform": p.source_platform,
        "etl_tool": p.etl_tool,
        "target_platform": p.target_platform,
        "configs": [
            {
                "id": c.id,
                "version_num": c.version_num,
                "config_hash": c.config_hash,
                "is_active": c.is_active,
                "raw_yaml": c.raw_yaml,
            }
            for c in cfgs
        ],
    }


@router.post("/secrets")
def store_secret(
    body: SecretSetRequest,
    _user: CurrentUser = Depends(require_perm(Permission.MANAGE_CONFIG)),
) -> dict[str, str]:
    vault = get_vault()
    vault.set_secret(body.reference, body.value)
    return {"status": "stored", "reference": body.reference}
