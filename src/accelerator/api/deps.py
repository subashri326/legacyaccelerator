"""FastAPI dependencies for DB sessions, JWT authentication, and RBAC."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from accelerator.core.exceptions import AuthenticationError
from accelerator.core.security import Permission, decode_access_token, require_permission
from accelerator.db.models import User
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


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> CurrentUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("Missing Bearer access token")
    token = authorization.split(" ", 1)[1].strip()
    claims = decode_access_token(token)
    username = str(claims.get("sub", ""))
    user = db.query(User).filter(User.username == username, User.is_active.is_(True)).first()
    if user is None:
        raise AuthenticationError("User not found or inactive")
    return CurrentUser(username=user.username, email=user.email, role=user.role_name)


def require_perm(permission: Permission) -> Callable[[CurrentUser], CurrentUser]:
    def _guard(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        require_permission(user.role, permission)
        return user

    return _guard
