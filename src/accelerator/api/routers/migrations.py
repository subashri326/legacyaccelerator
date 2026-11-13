"""Migrations (projects) built from saved connections, file uploads and live runs.

Creating a migration writes the same versioned client configuration a person could write by
hand, so everything stays reproducible; the web app just fills it in from the chosen source,
destination and options.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from accelerator.api.deps import (
    CurrentUser,
    get_db,
    require_perm,
    require_project_perm,
    visible_project_ids,
)
from accelerator.api.routers.connections import client_for, connection_for
from accelerator.api.routers.projects import _check_config_is_safe
from accelerator.config.client_config import parse_client_config_yaml
from accelerator.config.settings import get_settings
from accelerator.connectors.catalog import CATALOG
from accelerator.core.exceptions import ConfigError, NotFoundError, UnsafePathError
from accelerator.core.paths import MAX_UPLOAD_BYTES, check_import_path, save_upload
from accelerator.core.security import Permission, RoleName
from accelerator.db.models import (
    Client,
    Connection,
    ObjectStatus,
    PipelineRun,
    Project,
    ProjectMember,
)
from accelerator.db.repository import ensure_project_from_config
from accelerator.orchestration.live_runs import STAGES, run_view, start_run

router = APIRouter(prefix="/api/v1", tags=["migrations"])


class NamingOptions(BaseModel):
    schema_template: str = "{layer}_{source_schema}"
    table_case: Literal["upper", "lower", "preserve"] = "upper"
    column_case: Literal["upper", "lower", "preserve"] = "lower"
    layer: str = "curated"


class MigrationRequest(BaseModel):
    client: str
    name: str = Field(min_length=1, max_length=200)
    source_connection_id: str
    destination_connection_id: str
    schemas: list[str] = Field(default_factory=list)
    catalog_file: str | None = None  # an uploaded DDL export (instead of reading the database)
    etl_files: list[str] = Field(default_factory=list)  # uploaded job exports
    naming: NamingOptions = Field(default_factory=NamingOptions)
    reconciliation_level: Literal["L1", "L2", "L3", "L4"] = "L2"
    numeric_tolerance: float = 0.0
    chunk_rows: int = Field(default=50_000, ge=10, le=5_000_000)


class RunRequest(BaseModel):
    stages: list[str] = Field(default_factory=lambda: list(STAGES))


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "migration"


def _etl_tool(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".dtsx", ".ispac"):
        return "ssis"
    if ext in (".dsx", ".isx"):
        return "datastage"
    if ext == ".xml":
        head = path.read_bytes()[:600].decode("utf-8", errors="ignore")
        if "<DSExport" in head:
            return "datastage"
        if "DTS:Executable" in head:
            return "ssis"
        return "informatica"
    return "sql"


def _client_upload(client: Client, path: str) -> Path:
    p = check_import_path(path)
    folder = (get_settings().upload_dir / client.id).resolve()
    if folder not in p.parents:
        raise UnsafePathError("Files must be uploaded for this client first")
    return p


# --- uploads -----------------------------------------------------------------------------------
@router.post("/clients/{client}/uploads")
async def upload(
    client: str,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_perm(Permission.MANAGE_CONFIG)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    c = client_for(db, user, client)
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    dest = save_upload(c.id, file.filename, data)
    return {"path": str(dest), "name": dest.name, "size": len(data), "tool": _etl_tool(dest)}


# --- migrations --------------------------------------------------------------------------------
@router.post("/migrations")
def create_migration(
    body: MigrationRequest,
    user: CurrentUser = Depends(require_perm(Permission.MANAGE_CONFIG)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    client = client_for(db, user, body.client)
    src = connection_for(db, user, body.source_connection_id)
    dst = connection_for(db, user, body.destination_connection_id)
    if src.client_id != client.id or dst.client_id != client.id:
        raise ConfigError("Both connections must belong to this client")
    if src.role != "source" or dst.role != "target":
        raise ConfigError("Choose a source for the source and a destination for the destination")
    src_db = CATALOG[src.platform].database
    platform = (
        dst.properties.get("settings", {}).get("acts_as") or "snowflake"
        if dst.platform == "duckdb"
        else CATALOG[dst.platform].database
    )
    etl_paths = [_client_upload(client, f) for f in body.etl_files]
    tools = sorted({_etl_tool(p) for p in etl_paths}) or ["none"]
    cfg: dict[str, Any] = {
        "client": client.slug,
        "project": _slug(body.name),
        "environment": "dev",
        "source": {
            "database": src_db,
            "etl": tools,
            "connection": src.secret_ref,
            "schemas": body.schemas,
            "etl_export_paths": [str(p) for p in etl_paths],
        },
        "target": {
            "platform": platform,
            "output": "dbt",
            "connection": dst.secret_ref,
            "database": dst.properties.get("settings", {}).get("database", "MODERN_DW"),
            "warehouse": dst.properties.get("settings", {}).get("warehouse", "COMPUTE_WH"),
        },
        "naming": {
            "schema": body.naming.schema_template,
            "table_case": body.naming.table_case,
            "column_case": body.naming.column_case,
            "layer_default": body.naming.layer,
        },
        "reconciliation": {
            "default_level": body.reconciliation_level,
            "numeric_tolerance": body.numeric_tolerance,
        },
        "movement": {"mover": "parquet", "partition_size_rows": body.chunk_rows},
    }
    if body.catalog_file:
        cfg["source"]["catalog_bundle_path"] = str(_client_upload(client, body.catalog_file))
    raw = yaml.safe_dump(cfg, sort_keys=False)
    parsed, _ = parse_client_config_yaml(raw)
    _check_config_is_safe(parsed)
    exists = (
        db.query(Project)
        .filter(Project.client_id == client.id, Project.slug == parsed.project)
        .first()
    )
    if exists is not None:
        raise ConfigError(f"A migration called '{body.name}' already exists for this client")
    project, cfg_ver, _ = ensure_project_from_config(
        db, raw, actor=user.username, actor_role=user.role
    )
    project.name = body.name
    if user.role != RoleName.ADMIN.value:
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
        "id": project.id,
        "name": project.name,
        "config_version": cfg_ver.version_num,
        "config_yaml": raw,
    }


def _saved(db: Session, project: Project) -> dict[str, Any]:
    """The saved source/destination connections a migration uses (matched by vault reference)."""
    out: dict[str, Any] = {"source": None, "destination": None}
    for pc in db.query(Connection).filter(Connection.project_id == project.id):
        saved = (
            db.query(Connection)
            .filter(
                Connection.client_id == project.client_id,
                Connection.project_id.is_(None),
                Connection.secret_ref == pc.secret_ref,
            )
            .first()
        )
        key = "source" if pc.role == "source" else "destination"
        out[key] = {
            "id": saved.id if saved else None,
            "name": saved.name if saved else pc.name,
            "type": saved.platform if saved else pc.platform,
        }
    return out


def _summary(db: Session, project: Project) -> dict[str, Any]:
    states = Counter(
        s.state for s in db.query(ObjectStatus).filter(ObjectStatus.project_id == project.id)
    )
    blocked = (
        db.query(ObjectStatus)
        .filter(ObjectStatus.project_id == project.id, ObjectStatus.blocker_reason.isnot(None))
        .count()
    )
    last = (
        db.query(PipelineRun)
        .filter(PipelineRun.project_id == project.id)
        .order_by(PipelineRun.created_at.desc())
        .first()
    )
    client = db.get(Client, project.client_id)
    return {
        "id": project.id,
        "name": project.name,
        "client": client.slug if client else "",
        "client_name": client.name if client else "",
        "source_platform": project.source_platform,
        "target_platform": project.target_platform,
        "status": project.status,
        "objects": sum(states.values()),
        "by_state": dict(states),
        "blocked": blocked,
        "last_run": run_view(last) if last else None,
        **_saved(db, project),
    }


@router.get("/migrations")
def list_migrations(
    user: CurrentUser = Depends(require_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    q = db.query(Project).order_by(Project.created_at.desc())
    visible = visible_project_ids(db, user)
    if visible is not None:
        q = q.filter(Project.id.in_(visible))
    return [_summary(db, p) for p in q.all()]


@router.get("/migrations/{project_id}")
def get_migration(
    project_id: str,
    _u: CurrentUser = Depends(require_project_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    p = db.get(Project, project_id)
    if p is None:
        raise NotFoundError(f"Migration '{project_id}' not found")
    return _summary(db, p)


# --- runs --------------------------------------------------------------------------------------
@router.post("/projects/{project_id}/runs")
def start(
    project_id: str,
    body: RunRequest,
    user: CurrentUser = Depends(require_project_perm(Permission.RUN_CONVERT)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    run_id = start_run(project_id, user.username, user.role, body.stages)
    db.expire_all()
    run = db.get(PipelineRun, run_id)
    assert run is not None
    return run_view(run)


@router.get("/projects/{project_id}/runs")
def list_runs(
    project_id: str,
    limit: int = 20,
    _u: CurrentUser = Depends(require_project_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    runs = (
        db.query(PipelineRun)
        .filter(PipelineRun.project_id == project_id)
        .order_by(PipelineRun.created_at.desc())
        .limit(min(limit, 100))
    )
    return [run_view(r) for r in runs]


@router.get("/projects/{project_id}/runs/{run_id}")
def get_run(
    project_id: str,
    run_id: str,
    _u: CurrentUser = Depends(require_project_perm(Permission.VIEW_REPORTS)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    run = db.get(PipelineRun, run_id)
    if run is None or run.project_id != project_id:
        raise NotFoundError(f"Run '{run_id}' not found")
    return run_view(run)
