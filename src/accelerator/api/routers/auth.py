"""Authentication and RBAC user management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from accelerator.api.deps import CurrentUser, get_current_user, get_db, require_perm
from accelerator.api.schemas import CreateUserRequest, LoginRequest, TokenResponse
from accelerator.config.settings import get_settings
from accelerator.core.exceptions import AuthenticationError
from accelerator.core.security import (
    ROLE_PERMISSIONS,
    Permission,
    RoleName,
    create_access_token,
    hash_password,
    verify_password,
)
from accelerator.db.models import User
from accelerator.db.repository import record_audit

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    if get_settings().auth_mode == "oidc":
        raise AuthenticationError("Password sign-in is disabled; sign in through single sign-on")
    user = db.query(User).filter(User.username == body.username, User.is_active.is_(True)).first()
    if user is None or not verify_password(body.password, user.password_hash):
        record_audit(
            db,
            actor=body.username[:120],
            actor_role="unknown",
            action="auth.login_failed",
            resource_type="app_user",
            resource_id=body.username[:200],
        )
        db.commit()
        raise AuthenticationError("Invalid username or password")

    token = create_access_token(subject=user.username, role=user.role_name, email=user.email)
    perms = sorted(p.value for p in ROLE_PERMISSIONS.get(RoleName(user.role_name), set()))
    return TokenResponse(
        access_token=token,
        username=user.username,
        role=user.role_name,
        permissions=perms,
    )


@router.get("/me")
def me(user: CurrentUser = Depends(get_current_user)) -> dict[str, object]:
    perms = sorted(p.value for p in ROLE_PERMISSIONS.get(RoleName(user.role), set()))
    return {"username": user.username, "email": user.email, "role": user.role, "permissions": perms}


@router.get("/users")
def list_users(
    _user: CurrentUser = Depends(require_perm(Permission.MANAGE_USERS)),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    users = db.query(User).order_by(User.username).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "full_name": u.full_name,
            "role_name": u.role_name,
            "is_active": u.is_active,
        }
        for u in users
    ]


@router.post("/users")
def create_user(
    body: CreateUserRequest,
    actor: CurrentUser = Depends(require_perm(Permission.MANAGE_USERS)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    RoleName(body.role_name)
    user = User(
        username=body.username,
        email=body.email,
        full_name=body.full_name,
        role_name=body.role_name,
        password_hash=hash_password(body.password),
        created_by=actor.username,
    )
    db.add(user)
    db.flush()
    record_audit(
        db,
        actor=actor.username,
        actor_role=actor.role,
        action="user.created",
        resource_type="app_user",
        resource_id=user.id,
        details={"username": user.username, "role": user.role_name},
    )
    return {"id": user.id, "username": user.username, "role_name": user.role_name}
