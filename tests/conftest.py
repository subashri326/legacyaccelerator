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
