"""Database engine, session factory, and bootstrap initialization."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from accelerator.config.settings import get_settings
from accelerator.core.security import ROLE_PERMISSIONS, RoleName, hash_password
from accelerator.db.base import Base
from accelerator.db.models import Role, User


def _build_engine(database_url: str | None = None) -> Engine:
    settings = get_settings()
    url = database_url or settings.database_url
    connect_args: dict[str, object] = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        if url.startswith("sqlite:///"):
            db_file = Path(url.removeprefix("sqlite:///"))
            if str(db_file) != ":memory:":
                db_file.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, echo=settings.db_echo, future=True, connect_args=connect_args)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_fk(dbapi_conn, _conn_record):  # type: ignore[no-untyped-def]
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


_engines: dict[str, Engine] = {}
_session_factories: dict[str, sessionmaker[Session]] = {}


def get_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    if url not in _engines:
        _engines[url] = _build_engine(url)
    return _engines[url]


def get_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    url = database_url or get_settings().database_url
    if url not in _session_factories:
        _session_factories[url] = sessionmaker(
            bind=get_engine(url), autoflush=False, autocommit=False, expire_on_commit=False
        )
    return _session_factories[url]


@contextmanager
def session_scope(database_url: str | None = None) -> Iterator[Session]:
    factory = get_session_factory(database_url)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(database_url: str | None = None, seed_defaults: bool = True) -> Engine:
    """Create schema tables if missing and seed default RBAC roles and bootstrap users."""
    engine = get_engine(database_url)
    Base.metadata.create_all(bind=engine)
    if seed_defaults:
        with session_scope(database_url) as db:
            seed_roles_and_admin(db)
    return engine


def seed_roles_and_admin(db: Session) -> None:
    """Idempotently seed all 6 RBAC roles and default local demo users."""
    for role_enum in RoleName:
        existing = db.query(Role).filter(Role.name == role_enum.value).first()
        perms = sorted(p.value for p in ROLE_PERMISSIONS[role_enum])
        if existing is None:
            db.add(
                Role(
                    name=role_enum.value,
                    description=f"Standard {role_enum.value} role",
                    permissions=perms,
                    created_by="system",
                )
            )

    settings = get_settings()
    default_users = [
        ("admin", "admin@accelerator.local", "Platform Admin", RoleName.ADMIN.value),
        ("lead", "lead@accelerator.local", "Migration Lead", RoleName.MIGRATION_LEAD.value),
        ("engineer", "engineer@accelerator.local", "Data Engineer", RoleName.ENGINEER.value),
        ("reviewer", "reviewer@accelerator.local", "Code Reviewer", RoleName.REVIEWER.value),
        ("approver", "approver@accelerator.local", "Client Data Owner", RoleName.CLIENT_APPROVER.value),
        ("viewer", "viewer@accelerator.local", "Stakeholder Viewer", RoleName.VIEWER.value),
    ]
    for username, email, full_name, role_name in default_users:
        if db.query(User).filter(User.username == username).first() is None:
            db.add(
                User(
                    username=username,
                    email=email,
                    full_name=full_name,
                    role_name=role_name,
                    password_hash=hash_password(settings.default_admin_password),
                    created_by="system",
                )
            )
