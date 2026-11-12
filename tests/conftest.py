"""Shared pytest fixtures providing isolated DB, artifact store, and vault per test."""

from __future__ import annotations

from pathlib import Path

import pytest

from accelerator.config.settings import get_settings
from accelerator.db.session import init_db


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "metadata.db"
    art_root = tmp_path / "artifacts"
    vault_file = tmp_path / "vault.json"

    monkeypatch.setenv("L2C_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("L2C_STORAGE_BACKEND", "local")
    monkeypatch.setenv("L2C_STORAGE_LOCAL_ROOT", str(art_root))
    monkeypatch.setenv("L2C_VAULT_BACKEND", "local")
    monkeypatch.setenv("L2C_VAULT_LOCAL_FILE", str(vault_file))
    monkeypatch.setenv("L2C_JWT_SECRET", "test-secret-key-at-least-32-bytes-long")

    get_settings.cache_clear()
    init_db(f"sqlite:///{db_path.as_posix()}")
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture()
def pg_database():  # type: ignore[no-untyped-def]
    """A fresh, empty PostgreSQL database (needs L2C_TEST_POSTGRES_URL); dropped afterwards."""
    import os
    import uuid

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    base = os.getenv("L2C_TEST_POSTGRES_URL")
    if not base:
        pytest.skip("set L2C_TEST_POSTGRES_URL to run PostgreSQL tests")
    name = f"l2c_{uuid.uuid4().hex[:10]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"CREATE DATABASE {name}"))
    url = make_url(base).set(database=name).render_as_string(hide_password=False)
    yield url
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    admin.dispose()
