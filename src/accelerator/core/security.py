"""RBAC, JWT/OIDC token management, password hashing, and L4 row masking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import bcrypt
import jwt

from accelerator.config.settings import get_settings
from accelerator.core.exceptions import AuthenticationError, AuthorizationError


class RoleName(StrEnum):
    ADMIN = "admin"
    MIGRATION_LEAD = "migration_lead"
    ENGINEER = "engineer"
    REVIEWER = "reviewer"
    CLIENT_APPROVER = "client_approver"
    VIEWER = "viewer"


class Permission(StrEnum):
    MANAGE_USERS = "manage_users"
    MANAGE_PLUGINS = "manage_plugins"
    MANAGE_CONFIG = "manage_config"
    RUN_DISCOVER = "run_discover"
    RUN_ASSESS = "run_assess"
    PLAN_WAVES = "plan_waves"
    RUN_CONVERT = "run_convert"
    REVIEW_RESIDUE = "review_residue"
    DEPLOY_CODE = "deploy_code"
    RUN_MOVE = "run_move"
    RUN_RECONCILE = "run_reconcile"
    SIGN_OFF = "sign_off"
    VIEW_REPORTS = "view_reports"
    VIEW_AUDIT = "view_audit"


ROLE_PERMISSIONS: dict[RoleName, set[Permission]] = {
    RoleName.ADMIN: set(Permission),
    RoleName.MIGRATION_LEAD: {
        Permission.MANAGE_CONFIG,
        Permission.RUN_DISCOVER,
        Permission.RUN_ASSESS,
        Permission.PLAN_WAVES,
        Permission.RUN_CONVERT,
        Permission.DEPLOY_CODE,
        Permission.RUN_MOVE,
        Permission.RUN_RECONCILE,
        Permission.VIEW_REPORTS,
        Permission.VIEW_AUDIT,
    },
    RoleName.ENGINEER: {
        Permission.RUN_DISCOVER,
        Permission.RUN_ASSESS,
        Permission.RUN_CONVERT,
        Permission.REVIEW_RESIDUE,
        Permission.DEPLOY_CODE,
        Permission.RUN_MOVE,
        Permission.RUN_RECONCILE,
        Permission.VIEW_REPORTS,
    },
    RoleName.REVIEWER: {
        Permission.REVIEW_RESIDUE,
        Permission.VIEW_REPORTS,
        Permission.VIEW_AUDIT,
    },
    RoleName.CLIENT_APPROVER: {
        Permission.SIGN_OFF,
        Permission.VIEW_REPORTS,
        Permission.VIEW_AUDIT,
    },
    RoleName.VIEWER: {
        Permission.VIEW_REPORTS,
    },
}


def has_permission(role: str | RoleName, permission: Permission) -> bool:
    try:
        role_enum = RoleName(role)
    except ValueError:
        return False
    return permission in ROLE_PERMISSIONS.get(role_enum, set())


def require_permission(role: str | RoleName, permission: Permission) -> None:
    if not has_permission(role, permission):
        raise AuthorizationError(f"Role '{role}' lacks permission '{permission.value}'")


def hash_password(plain_password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(plain_password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(
    subject: str,
    role: str,
    email: str = "",
    expires_minutes: int | None = None,
) -> str:
    settings = get_settings()
    ttl = expires_minutes or settings.access_token_expire_minutes
    now = datetime.now(UTC)
    payload = {
        "sub": subject,
        "role": role,
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "exp"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError(f"Invalid or expired token: {exc}") from exc


def mask_value(value: Any) -> Any:
    """Mask sensitive scalar values while preserving basic shape for diff debugging."""
    if value is None:
        return None
    text = str(value)
    if len(text) <= 4:
        return "***"
    return f"{text[:2]}{'*' * min(len(text) - 4, 8)}{text[-2:]}"


def mask_row_sample(row: dict[str, Any], masked_columns: list[str]) -> dict[str, Any]:
    """Mask sensitive columns in L4 reconciliation diff samples (Section 7.3)."""
    masked_lower = {c.lower() for c in masked_columns}
    output: dict[str, Any] = {}
    for key, val in row.items():
        key_l = key.lower()
        if key_l in masked_lower or any(tok in key_l for tok in ("ssn", "secret", "password", "token")):
            output[key] = mask_value(val)
        else:
            output[key] = val
    return output
