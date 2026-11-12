"""Single sign-on (OIDC): verify identity-provider tokens and map them to product users.

With ``auth_mode = "oidc"`` the API trusts only tokens issued by ``oidc_issuer`` for
``oidc_audience``, signed with a key published in the issuer's JWKS. The user's product role
comes from a claim (``oidc_role_claim``, e.g. Entra ID app roles or groups) mapped through
``oidc_role_map``; the identity provider stays the source of truth, so the role is refreshed on
every request. Local passwords are not accepted in this mode.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient
from sqlalchemy.orm import Session

from accelerator.config.settings import get_settings
from accelerator.core.exceptions import AuthenticationError
from accelerator.core.security import RoleName

# When a user's claims map to several roles, the most privileged wins.
ROLE_PRIORITY = [
    RoleName.ADMIN,
    RoleName.MIGRATION_LEAD,
    RoleName.ENGINEER,
    RoleName.REVIEWER,
    RoleName.CLIENT_APPROVER,
    RoleName.VIEWER,
]
UNUSABLE_PASSWORD = "!sso-only"


@lru_cache(maxsize=4)
def _discover_jwks_url(issuer: str) -> str:
    resp = httpx.get(f"{issuer.rstrip('/')}/.well-known/openid-configuration", timeout=10.0)
    resp.raise_for_status()
    return str(resp.json()["jwks_uri"])


@lru_cache(maxsize=4)
def jwks_client(url: str) -> PyJWKClient:
    return PyJWKClient(url, cache_keys=True)


def verify_oidc_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.oidc_issuer or not settings.oidc_audience:
        raise AuthenticationError(
            "SSO is enabled but oidc_issuer / oidc_audience are not configured"
        )
    try:
        url = settings.oidc_jwks_url or _discover_jwks_url(settings.oidc_issuer)
        key = jwks_client(url).get_signing_key_from_jwt(token).key
        return jwt.decode(
            token,
            key,
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384"],
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            options={"require": ["exp", "iss", "aud"]},
        )
    except (jwt.PyJWTError, httpx.HTTPError, KeyError) as exc:
        raise AuthenticationError(f"Invalid SSO token: {exc}") from exc


def role_from_claims(claims: dict[str, Any]) -> str:
    settings = get_settings()
    raw = claims.get(settings.oidc_role_claim) or []
    values = [raw] if isinstance(raw, str) else list(raw)
    mapped = {settings.oidc_role_map.get(v) for v in values} - {None}
    for role in ROLE_PRIORITY:
        if role.value in mapped:
            return role.value
    return RoleName(settings.oidc_default_role).value


def provision_user(db: Session, claims: dict[str, Any]) -> Any:
    """Find or create the product user for an SSO identity and refresh their role."""
    from accelerator.db.models import User
    from accelerator.db.repository import record_audit

    settings = get_settings()
    username = str(claims.get(settings.oidc_username_claim) or claims.get("email") or claims["sub"])
    email = str(claims.get("email") or claims.get("upn") or f"{username}@sso.local")
    role = role_from_claims(claims)
    user = db.query(User).filter(User.username == username).first()
    if user is None and db.query(User).filter(User.email == email).first() is not None:
        raise AuthenticationError(
            f"Email {email} already belongs to another account; an admin must link or rename it"
        )
    if user is None:
        user = User(
            username=username,
            email=email,
            full_name=str(claims.get("name") or username),
            role_name=role,
            password_hash=UNUSABLE_PASSWORD,
            created_by="sso",
        )
        db.add(user)
        db.flush()
        record_audit(
            db,
            actor=username,
            actor_role=role,
            action="user.sso_provisioned",
            resource_type="app_user",
            resource_id=user.id,
            details={"issuer": claims.get("iss")},
        )
    elif user.role_name != role:
        record_audit(
            db,
            actor=username,
            actor_role=role,
            action="user.sso_role_changed",
            resource_type="app_user",
            resource_id=user.id,
            details={"from": user.role_name, "to": role},
        )
        user.role_name = role
    if not user.is_active:
        raise AuthenticationError("User is deactivated")
    return user
