"""Idempotent metadata repository helpers and audit trail recording."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from accelerator.config.client_config import ClientConfig, parse_client_config_yaml
from accelerator.db.models import (
    AuditLog,
    Client,
    ConfigVersion,
    Connection,
    Environment,
    Project,
)


def record_audit(
    db: Session,
    *,
    actor: str,
    actor_role: str,
    action: str,
    resource_type: str,
    resource_id: str,
    project_id: str | None = None,
    config_version_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    entry = AuditLog(
        project_id=project_id,
        actor=actor,
        actor_role=actor_role,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        config_version_id=config_version_id,
        details=details or {},
    )
    db.add(entry)
    db.flush()
    return entry


def ensure_project_from_config(
    db: Session,
    raw_yaml: str,
    actor: str = "system",
    actor_role: str = "admin",
) -> tuple[Project, ConfigVersion, ClientConfig]:
    """Idempotently create or update Client, Project, Environments, Connections, and ConfigVersion."""
    cfg, cfg_hash = parse_client_config_yaml(raw_yaml)

    client = db.query(Client).filter(Client.slug == cfg.client).first()
    if client is None:
        client = Client(slug=cfg.client, name=cfg.client.replace("_", " ").title(), created_by=actor)
        db.add(client)
        db.flush()

    project = (
        db.query(Project)
        .filter(Project.client_id == client.id, Project.slug == cfg.project)
        .first()
    )
    if project is None:
        project = Project(
            client_id=client.id,
            slug=cfg.project,
            name=cfg.project.replace("_", " ").title(),
            source_platform=cfg.source.database,
            etl_tools=list(cfg.source.etl_tools),
            target_platform=cfg.target.platform,
            status="active" if cfg.target.platform else "comparing",
            created_by=actor,
        )
        db.add(project)
        db.flush()
    else:
        project.source_platform = cfg.source.database
        project.etl_tools = list(cfg.source.etl_tools)
        project.target_platform = cfg.target.platform
        if project.status in ("active", "comparing"):
            project.status = "active" if cfg.target.platform else "comparing"

    env = (
        db.query(Environment)
        .filter(Environment.project_id == project.id, Environment.name == cfg.environment)
        .first()
    )
    if env is None:
        env = Environment(
            project_id=project.id,
            name=cfg.environment,
            is_default=True,
            created_by=actor,
        )
        db.add(env)
        db.flush()

    for conn_name, role, platform, ref in [
        ("source_default", "source", cfg.source.database, cfg.source.connection),
        ("target_default", "target", cfg.target.platform or "undecided", cfg.target.connection),
    ]:
        if not ref:
            continue
        existing_conn = (
            db.query(Connection)
            .filter(
                Connection.project_id == project.id,
                Connection.environment_id == env.id,
                Connection.name == conn_name,
            )
            .first()
        )
        if existing_conn is None:
            db.add(
                Connection(
                    project_id=project.id,
                    environment_id=env.id,
                    name=conn_name,
                    role=role,
                    platform=platform,
                    secret_ref=ref,
                    created_by=actor,
                )
            )
        else:
            existing_conn.platform = platform
            existing_conn.secret_ref = ref

    active_cfg = (
        db.query(ConfigVersion)
        .filter(ConfigVersion.project_id == project.id, ConfigVersion.is_active.is_(True))
        .order_by(ConfigVersion.version_num.desc())
        .first()
    )
    if active_cfg and active_cfg.config_hash == cfg_hash:
        return project, active_cfg, cfg

    max_ver = (
        db.query(ConfigVersion)
        .filter(ConfigVersion.project_id == project.id)
        .order_by(ConfigVersion.version_num.desc())
        .first()
    )
    next_ver = (max_ver.version_num + 1) if max_ver else 1
    db.query(ConfigVersion).filter(ConfigVersion.project_id == project.id).update({"is_active": False})

    new_cfg = ConfigVersion(
        project_id=project.id,
        version_num=next_ver,
        config_hash=cfg_hash,
        raw_yaml=raw_yaml,
        parsed_json=cfg.model_dump(mode="json"),
        is_active=True,
        created_by=actor,
    )
    db.add(new_cfg)
    db.flush()

    record_audit(
        db,
        actor=actor,
        actor_role=actor_role,
        action="config.registered",
        resource_type="config_version",
        resource_id=new_cfg.id,
        project_id=project.id,
        config_version_id=new_cfg.id,
        details={"version_num": next_ver, "config_hash": cfg_hash},
    )
    return project, new_cfg, cfg


def get_active_config(db: Session, project_id: str) -> tuple[ConfigVersion, ClientConfig]:
    cfg_row = (
        db.query(ConfigVersion)
        .filter(ConfigVersion.project_id == project_id, ConfigVersion.is_active.is_(True))
        .order_by(ConfigVersion.version_num.desc())
        .first()
    )
    if cfg_row is None:
        raise ValueError(f"No active config version for project '{project_id}'")
    return cfg_row, ClientConfig.model_validate(cfg_row.parsed_json)
