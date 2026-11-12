"""The metadata schema has one source of truth: migrations must build exactly what the models describe."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from accelerator.config.client_config import ClientConfig, parse_client_config_yaml
from accelerator.core.exceptions import ConfigError
from accelerator.db.base import Base
from accelerator.db.models import Project
from accelerator.db.repository import ensure_project_from_config
from accelerator.db.session import init_db, session_scope


def _assert_migrations_match_models(url: str) -> None:
    init_db(url)
    init_db(url)  # re-running must be a no-op
    with create_engine(url).connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(ctx, Base.metadata) == []
        tables = set(inspect(conn).get_table_names()) - {"alembic_version"}
        assert tables == set(Base.metadata.tables)


def test_migrations_match_models_sqlite(tmp_path: Path) -> None:
    _assert_migrations_match_models(f"sqlite:///{(tmp_path / 'schema.db').as_posix()}")


@pytest.mark.skipif(not os.getenv("L2C_TEST_POSTGRES_URL"), reason="set L2C_TEST_POSTGRES_URL to run")
def test_migrations_match_models_postgres() -> None:
    _assert_migrations_match_models(os.environ["L2C_TEST_POSTGRES_URL"])


def test_etl_accepts_one_tool_or_a_list() -> None:
    base = {"client": "c", "target": {"platform": "snowflake"}}
    one = ClientConfig.model_validate({**base, "source": {"database": "sqlserver", "etl": "ssis"}})
    many = ClientConfig.model_validate(
        {**base, "source": {"database": "sqlserver", "etl": ["ssis", "informatica", "datastage"]}}
    )
    assert one.source.etl_tools == ["ssis"] and one.source.etl == "ssis"
    assert many.source.etl_tools == ["ssis", "informatica", "datastage"] and many.source.etl == "ssis"


def test_project_stays_comparing_until_target_chosen(isolated_env: Path) -> None:
    undecided = "client: acme\nproject: dw\nsource:\n  database: sqlserver\n  etl: [ssis, informatica]\n"
    cfg, _ = parse_client_config_yaml(undecided)
    with pytest.raises(ConfigError):
        cfg.require_target()

    with session_scope() as db:
        proj, _, _ = ensure_project_from_config(db, undecided)
        pid = proj.id
    with session_scope() as db:
        p = db.get(Project, pid)
        assert p.status == "comparing" and p.target_platform is None
        assert p.etl_tools == ["ssis", "informatica"]

    with session_scope() as db:
        ensure_project_from_config(db, undecided + "target:\n  platform: fabric\n")
    with session_scope() as db:
        p = db.get(Project, pid)
        assert p.status == "active" and p.target_platform == "fabric"
