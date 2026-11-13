"""Saved connections (sources and destinations), like Airbyte's Sources and Destinations pages.

A connection belongs to a client and is reused by that client's migrations. Its form values are
stored in two places: ordinary settings (host, database...) in the metadata database, and secret
values (passwords, tokens) plus the full connection string only in the secrets vault.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from accelerator.api.deps import CurrentUser, get_db, require_perm, visible_project_ids
from accelerator.config.settings import get_settings
from accelerator.connectors.base import open_connector
from accelerator.connectors.catalog import (
    CATALOG,
    ConnectorType,
    get_type,
    split_secrets,
    test_connection,
    validate_settings,
)
from accelerator.core.exceptions import AuthorizationError, ConfigError, NotFoundError
from accelerator.core.secrets import get_vault
from accelerator.core.security import Permission, RoleName
from accelerator.db.base import utcnow
from accelerator.db.models import Client, Connection, Project
from accelerator.db.repository import record_audit

router = APIRouter(prefix="/api/v1", tags=["connections"])


class ClientRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    industry: str = "general"


class ConnectionRequest(BaseModel):
    client: str  # slug
    name: str = Field(min_length=1, max_length=120)
    type: str
    kind: str | None = None  # "source" or "destination"; needed for types that can be either
    settings: dict[str, Any] = Field(default_factory=dict)


class ConnectionUpdate(BaseModel):
    name: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)


class TestRequest(BaseModel):
    type: str
    settings: dict[str, Any] = Field(default_factory=dict)
    connection_id: str | None = None  # re-use stored secrets for fields left blank


# --- access ------------------------------------------------------------------------------------
def visible_clients(db: Session, user: CurrentUser) -> list[Client]:
    """Admins see every client; others see clients they created or have a project in."""
    q = db.query(Client).order_by(Client.name)
    if user.role == RoleName.ADMIN.value:
        return q.all()
    projects = visible_project_ids(db, user) or set()
    member_clients = (
        {p.client_id for p in db.query(Project).filter(Project.id.in_(projects))}
        if projects
        else set()
    )
    return [c for c in q.all() if c.id in member_clients or c.created_by == user.username]


def client_for(db: Session, user: CurrentUser, slug: str) -> Client:
    for c in visible_clients(db, user):
        if c.slug == slug:
            return c
    raise NotFoundError(f"Client '{slug}' not found")


def connection_for(db: Session, user: CurrentUser, connection_id: str) -> Connection:
    conn = db.get(Connection, connection_id)
    if conn is None or conn.client_id is None:
        raise NotFoundError(f"Connection '{connection_id}' not found")
    if conn.client_id not in {c.id for c in visible_clients(db, user)}:
        raise NotFoundError(f"Connection '{connection_id}' not found")
    return conn


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "client"


def _check_type_allowed(ct: ConnectorType) -> None:
    if ct.id == "duckdb" and not get_settings().is_dev:
        raise AuthorizationError("Local DuckDB files can only be used in a development install")


def _secret_refs(client: Client, conn_id: str) -> tuple[str, str]:
    base = f"vault://{client.slug}/connections/{conn_id}"
    return base, f"{base}/fields"


def _stored_secrets(conn: Connection) -> dict[str, Any]:
    try:
        return json.loads(get_vault().get_secret(f"{conn.secret_ref}/fields"))
    except Exception:
        return {}


def _merge(ct: ConnectorType, settings: dict[str, Any], conn: Connection | None) -> dict[str, Any]:
    """Form values, keeping stored secrets for secret fields the user left blank."""
    merged = dict(conn.properties.get("settings", {})) if conn else {}
    merged.update({k: v for k, v in settings.items() if v not in (None, "")})
    if conn is not None:
        for k, v in _stored_secrets(conn).items():
            if settings.get(k) in (None, ""):
                merged[k] = v
    return validate_settings(ct, merged)


def view(conn: Connection, client: Client | None = None) -> dict[str, Any]:
    ct = CATALOG.get(conn.platform)
    return {
        "id": conn.id,
        "name": conn.name,
        "client": client.slug if client else None,
        "kind": conn.role,
        "type": conn.platform,
        "type_name": ct.name if ct else conn.platform,
        "settings": conn.properties.get("settings", {}),  # never contains secrets
        "secret_fields_set": sorted(conn.properties.get("secret_fields", [])),
        "last_test_status": conn.last_test_status,
        "last_test_message": conn.last_test_message,
        "last_tested_at": conn.last_tested_at.isoformat() if conn.last_tested_at else None,
        "created_by": conn.created_by,
    }


# --- routes ------------------------------------------------------------------------------------
@router.get("/connector-types")
def connector_types(_u: CurrentUser = Depends(require_perm(Permission.VIEW_REPORTS))) -> list[dict]:
    allowed = get_settings().is_dev
    return [c.spec() for c in CATALOG.values() if allowed or c.id != "duckdb"]


@router.get("/clients")
def list_clients(
    user: CurrentUser = Depends(require_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return [
        {"id": c.id, "slug": c.slug, "name": c.name, "industry": c.industry}
        for c in visible_clients(db, user)
    ]


@router.post("/clients")
def create_client(
    body: ClientRequest,
    user: CurrentUser = Depends(require_perm(Permission.MANAGE_CONFIG)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    slug = _slug(body.name)
    if db.query(Client).filter(Client.slug == slug).first() is not None:
        raise ConfigError(f"A client called '{body.name}' already exists")
    c = Client(slug=slug, name=body.name, industry=body.industry, created_by=user.username)
    db.add(c)
    db.flush()
    record_audit(
        db,
        actor=user.username,
        actor_role=user.role,
        action="client.created",
        resource_type="client",
        resource_id=c.id,
    )
    db.commit()
    return {"id": c.id, "slug": c.slug, "name": c.name, "industry": c.industry}


@router.get("/connections")
def list_connections(
    client: str | None = None,
    kind: str | None = None,
    user: CurrentUser = Depends(require_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    clients = {c.id: c for c in visible_clients(db, user) if client in (None, c.slug)}
    q = db.query(Connection).filter(
        Connection.client_id.in_(clients), Connection.project_id.is_(None)
    )
    if kind:
        q = q.filter(Connection.role == kind)
    return [view(c, clients[c.client_id]) for c in q.order_by(Connection.name)]  # type: ignore[index]


@router.post("/connections/test")
def test_unsaved(
    body: TestRequest,
    user: CurrentUser = Depends(require_perm(Permission.MANAGE_CONFIG)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Test form values before saving (secrets left blank fall back to the saved connection)."""
    ct = get_type(body.type)
    _check_type_allowed(ct)
    existing = connection_for(db, user, body.connection_id) if body.connection_id else None
    values = _merge(ct, body.settings, existing)
    r = test_connection(ct, values)
    return {"ok": r.ok, "message": r.message, "seconds": round(r.seconds, 2)}


@router.post("/connections")
def create_connection(
    body: ConnectionRequest,
    user: CurrentUser = Depends(require_perm(Permission.MANAGE_CONFIG)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    client = client_for(db, user, body.client)
    ct = get_type(body.type)
    _check_type_allowed(ct)
    if (
        db.query(Connection)
        .filter(Connection.client_id == client.id, Connection.name == body.name)
        .first()
    ):
        raise ConfigError(f"'{body.name}' already exists for this client")
    values = validate_settings(ct, body.settings)
    plain, secrets = split_secrets(ct, values)
    if ct.kind == "both":
        if body.kind not in ("source", "destination"):
            raise ConfigError("Say whether this connection is a source or a destination")
        role = body.kind
    else:
        role = ct.kind
    kind = "target" if role == "destination" else "source"
    conn = Connection(
        client_id=client.id,
        project_id=None,
        name=body.name,
        role=kind,
        platform=ct.id,
        secret_ref="",
        created_by=user.username,
        properties={"settings": plain, "secret_fields": sorted(secrets)},
    )
    db.add(conn)
    db.flush()
    uri_ref, fields_ref = _secret_refs(client, conn.id)
    vault = get_vault()
    vault.set_secret(uri_ref, ct.build_uri(values))
    vault.set_secret(fields_ref, json.dumps(secrets))
    conn.secret_ref = uri_ref
    record_audit(
        db,
        actor=user.username,
        actor_role=user.role,
        action="connection.created",
        resource_type="connection",
        resource_id=conn.id,
        details={"type": ct.id, "name": body.name},
    )
    db.commit()
    return view(conn, client)


@router.put("/connections/{connection_id}")
def update_connection(
    connection_id: str,
    body: ConnectionUpdate,
    user: CurrentUser = Depends(require_perm(Permission.MANAGE_CONFIG)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    conn = connection_for(db, user, connection_id)
    ct = get_type(conn.platform)
    values = _merge(ct, body.settings, conn)
    plain, secrets = split_secrets(ct, values)
    vault = get_vault()
    vault.set_secret(conn.secret_ref, ct.build_uri(values))
    vault.set_secret(f"{conn.secret_ref}/fields", json.dumps(secrets))
    conn.properties = {"settings": plain, "secret_fields": sorted(secrets)}
    if body.name:
        conn.name = body.name
    conn.last_test_status = conn.last_test_message = None
    conn.last_tested_at = None
    record_audit(
        db,
        actor=user.username,
        actor_role=user.role,
        action="connection.updated",
        resource_type="connection",
        resource_id=conn.id,
    )
    db.commit()
    return view(conn, db.get(Client, conn.client_id))


@router.delete("/connections/{connection_id}")
def delete_connection(
    connection_id: str,
    user: CurrentUser = Depends(require_perm(Permission.MANAGE_CONFIG)),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    conn = connection_for(db, user, connection_id)
    used = (
        db.query(Connection)
        .filter(Connection.project_id.isnot(None), Connection.secret_ref == conn.secret_ref)
        .count()
    )
    if used:
        raise ConfigError(f"'{conn.name}' is used by {used} migration(s); remove those first")
    db.delete(conn)
    record_audit(
        db,
        actor=user.username,
        actor_role=user.role,
        action="connection.deleted",
        resource_type="connection",
        resource_id=connection_id,
    )
    db.commit()
    return {"deleted": connection_id}


@router.post("/connections/{connection_id}/test")
def test_saved(
    connection_id: str,
    user: CurrentUser = Depends(require_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    conn = connection_for(db, user, connection_id)
    ct = get_type(conn.platform)
    r = test_connection(ct, _merge(ct, {}, conn))
    conn.last_test_status = "passed" if r.ok else "failed"
    conn.last_test_message = r.message
    conn.last_tested_at = utcnow()
    db.commit()
    return {"ok": r.ok, "message": r.message, "seconds": round(r.seconds, 2)}


@router.get("/connections/{connection_id}/schemas")
def browse(
    connection_id: str,
    schema: str | None = None,
    user: CurrentUser = Depends(require_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Schemas a connection can see; with ?schema=, that schema's tables."""
    conn = connection_for(db, user, connection_id)
    with open_connector(conn.secret_ref, read_only=True) as c:
        if schema:
            return {"schema": schema, "tables": c.list_tables(schema)}
        return {"schemas": c.list_schemas()}
