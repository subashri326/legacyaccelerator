"""Projects, config versioning, membership, and secrets registration routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from accelerator.api.deps import (
    CurrentUser,
    get_current_user,
    get_db,
    project_role,
    require_perm,
    require_project_perm,
    visible_project_ids,
)
from accelerator.api.schemas import RegisterProjectYamlRequest, SecretSetRequest
from accelerator.config.client_config import ClientConfig, parse_client_config_yaml
from accelerator.core.exceptions import AuthorizationError, NotFoundError, UnsafePathError
from accelerator.core.paths import check_import_path
from accelerator.core.secrets import get_vault
from accelerator.core.security import Permission, RoleName, require_permission
from accelerator.db.models import Client, ConfigVersion, Project, ProjectMember, User
from accelerator.db.repository import ensure_project_from_config, record_audit

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


class MemberRequest(BaseModel):
    username: str
    role_name: str


def _check_config_is_safe(cfg: ClientConfig) -> None:
    """A config sent over the API must not point the server at arbitrary files or databases.

    File paths must sit inside the allowed import folders, and connections must be vault/env
    references (secrets are written only by admins). A direct URI such as
    sqlite:////app/.l2c_data/accelerator.db would otherwise let Discover read this product's own
    metadata - every client's data. The CLI, run by an operator, is not restricted.
    """
    for path in [
        cfg.source.catalog_bundle_path,
        cfg.source.usage_log_path,
        *cfg.source.etl_export_paths,
    ]:
        if path:
            check_import_path(path)
    for ref in (cfg.source.connection, cfg.target.connection):
        if ref and not ref.startswith(("vault://", "env://")):
            raise UnsafePathError(
                "Connections in a client config must be vault:// or env:// references; "
                "ask an admin to store the connection string as a secret"
            )


@router.get("")
def list_projects(
    user: CurrentUser = Depends(require_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    q = db.query(Project).order_by(Project.created_at.desc())
    visible = visible_project_ids(db, user)
    if visible is not None:
        q = q.filter(Project.id.in_(visible))
    clients = {c.id: c for c in db.query(Client).all()}
    return [
        {
            "id": p.id,
            "slug": p.slug,
            "name": p.name,
            "client_slug": clients[p.client_id].slug if p.client_id in clients else "",
            "source_platform": p.source_platform,
            "etl_tools": p.etl_tools,
            "target_platform": p.target_platform,
            "status": p.status,
        }
        for p in q.all()
    ]


@router.post("/register-yaml")
def register_project_yaml(
    body: RegisterProjectYamlRequest,
    user: CurrentUser = Depends(require_perm(Permission.MANAGE_CONFIG)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    # Updating an existing client project needs config rights *on that project*; otherwise the
    # name alone would let anyone overwrite another client's configuration.
    cfg, _ = parse_client_config_yaml(body.raw_yaml)
    _check_config_is_safe(cfg)
    existing = (
        db.query(Project)
        .join(Client, Client.id == Project.client_id)
        .filter(Client.slug == cfg.client, Project.slug == cfg.project)
        .first()
    )
    if existing is not None:
        role = project_role(db, user, existing.id)
        if role is None:
            raise AuthorizationError(
                "A project with this client and name exists and you are not a member of it"
            )
        require_permission(role, Permission.MANAGE_CONFIG)

    project, cfg_ver, cfg = ensure_project_from_config(
        db, body.raw_yaml, actor=user.username, actor_role=user.role
    )
    if existing is None and user.role != RoleName.ADMIN.value:
        db.add(
            ProjectMember(
                project_id=project.id,
                user_id=user.user_id,
                role_name=user.role,
                created_by=user.username,
            )
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
    _user: CurrentUser = Depends(require_project_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    p = db.get(Project, project_id)
    if p is None:
        raise NotFoundError(f"Project '{project_id}' not found")
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
        "etl_tools": p.etl_tools,
        "target_platform": p.target_platform,
        "status": p.status,
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


@router.get("/{project_id}/members")
def list_members(
    project_id: str,
    _user: CurrentUser = Depends(require_project_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    rows = (
        db.query(ProjectMember, User)
        .join(User, User.id == ProjectMember.user_id)
        .filter(ProjectMember.project_id == project_id)
        .order_by(User.username)
        .all()
    )
    return [
        {"username": u.username, "full_name": u.full_name, "role_name": m.role_name}
        for m, u in rows
    ]


@router.post("/{project_id}/members")
def add_member(
    project_id: str,
    body: MemberRequest,
    actor: CurrentUser = Depends(require_project_perm(Permission.MANAGE_CONFIG)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    role = RoleName(body.role_name)
    if role == RoleName.ADMIN:
        raise AuthorizationError("Admin is a platform role and cannot be granted per project")
    target = db.query(User).filter(User.username == body.username, User.is_active.is_(True)).first()
    if target is None:
        raise NotFoundError(f"User '{body.username}' not found")
    member = db.query(ProjectMember).filter_by(project_id=project_id, user_id=target.id).first()
    if member is None:
        db.add(
            ProjectMember(
                project_id=project_id,
                user_id=target.id,
                role_name=role.value,
                created_by=actor.username,
            )
        )
    else:
        member.role_name = role.value
    record_audit(
        db,
        actor=actor.username,
        actor_role=actor.role,
        action="project.member_set",
        resource_type="project_member",
        resource_id=f"{project_id}:{target.username}",
        project_id=project_id,
        details={"username": target.username, "role": role.value},
    )
    db.commit()
    return {"project_id": project_id, "username": target.username, "role_name": role.value}


@router.delete("/{project_id}/members/{username}")
def remove_member(
    project_id: str,
    username: str,
    actor: CurrentUser = Depends(require_project_perm(Permission.MANAGE_CONFIG)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    target = db.query(User).filter(User.username == username).first()
    member = (
        db.query(ProjectMember).filter_by(project_id=project_id, user_id=target.id).first()
        if target
        else None
    )
    if member is None:
        raise NotFoundError(f"'{username}' is not a member of this project")
    db.delete(member)
    record_audit(
        db,
        actor=actor.username,
        actor_role=actor.role,
        action="project.member_removed",
        resource_type="project_member",
        resource_id=f"{project_id}:{username}",
        project_id=project_id,
    )
    db.commit()
    return {"project_id": project_id, "removed": username}


@router.post("/secrets")
def store_secret(
    body: SecretSetRequest,
    user: CurrentUser = Depends(require_perm(Permission.MANAGE_USERS)),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Store a connection secret. Admin only: a secret can point at any client's systems."""
    get_vault().set_secret(body.reference, body.value)
    record_audit(
        db,
        actor=user.username,
        actor_role=user.role,
        action="secret.stored",
        resource_type="secret",
        resource_id=body.reference,
    )
    return {"status": "stored", "reference": body.reference}


__all__ = ["get_current_user", "router"]
