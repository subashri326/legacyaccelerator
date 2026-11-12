"""FastAPI dependencies: DB sessions, authentication (local JWT or SSO), and access checks.

Two layers of access:
* Platform permissions (managing users, plugins, registering clients) use the user's own role.
* Everything under /projects/{project_id} requires membership of that project, and uses the
  role the user holds *on that project*. Admins see every project. Non-members get 404, so
  they cannot tell another client's project exists.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from accelerator.config.settings import get_settings
from accelerator.core.exceptions import AuthenticationError, NotFoundError
from accelerator.core.security import Permission, RoleName, decode_access_token, require_permission
from accelerator.db.models import Project, ProjectMember, User
from accelerator.db.session import get_session_factory


def get_db() -> Iterator[Session]:
    factory = get_session_factory()
    db = factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@dataclass
class CurrentUser:
    username: str
    email: str
    role: str
    user_id: str = ""


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> CurrentUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("Missing Bearer access token")
    token = authorization.split(" ", 1)[1].strip()
    if get_settings().auth_mode == "oidc":
        from accelerator.core.oidc import provision_user, verify_oidc_token

        user = provision_user(db, verify_oidc_token(token))
    else:
        claims = decode_access_token(token)
        username = str(claims.get("sub", ""))
        user = db.query(User).filter(User.username == username, User.is_active.is_(True)).first()
        if user is None:
            raise AuthenticationError("User not found or inactive")
    return CurrentUser(
        username=user.username, email=user.email, role=user.role_name, user_id=user.id
    )


def require_perm(permission: Permission) -> Callable[..., CurrentUser]:
    """Platform-level permission, checked against the user's own role."""

    def _guard(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        require_permission(user.role, permission)
        return user

    return _guard


def project_role(db: Session, user: CurrentUser, project_id: str) -> str | None:
    """The role a user holds on a project: admins everywhere, others only where they are members."""
    if user.role == RoleName.ADMIN.value:
        return RoleName.ADMIN.value
    member = (
        db.query(ProjectMember)
        .filter(ProjectMember.project_id == project_id, ProjectMember.user_id == user.user_id)
        .first()
    )
    return member.role_name if member else None


def visible_project_ids(db: Session, user: CurrentUser) -> set[str] | None:
    """Projects a user may see; None means all (admin)."""
    if user.role == RoleName.ADMIN.value:
        return None
    rows = db.query(ProjectMember.project_id).filter(ProjectMember.user_id == user.user_id).all()
    return {r[0] for r in rows}


def require_project_perm(permission: Permission) -> Callable[..., CurrentUser]:
    """Membership of the path's project plus `permission` in the project role.

    The returned user carries the project role, so audit entries record what they acted as.
    """

    def _guard(
        project_id: str,
        user: CurrentUser = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> CurrentUser:
        role = project_role(db, user, project_id)
        if role is None or db.get(Project, project_id) is None:
            raise NotFoundError(f"Project '{project_id}' not found")
        require_permission(role, permission)
        return replace(user, role=role)

    return _guard
