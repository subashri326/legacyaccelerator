"""Tests for FastAPI REST endpoints, RBAC enforcement, Prometheus /metrics, and Typer CLI."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from accelerator.api.app import create_app
from accelerator.cli.main import app as cli_app
from accelerator.core.secrets import get_vault


def _setup_duckdb_vault(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "seed_legacy_db", Path("examples/acme_retail/seed_legacy_db.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    src_db, tgt_db = mod.seed_all(base_dir=tmp_path / "api_duckdb")
    vault = get_vault()
    vault.set_secret("vault://acme/sqlserver-ro", f"duckdb:///{src_db.as_posix()}")
    vault.set_secret("vault://acme/snowflake-deploy", f"duckdb:///{tgt_db.as_posix()}")


def test_api_rbac_and_full_flow(isolated_env: Path) -> None:
    _setup_duckdb_vault(isolated_env)
    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200

        # Login as viewer -> cannot register config or run pipeline (403)
        v_login = client.post(
            "/api/v1/auth/login", json={"username": "viewer", "password": "admin123!"}
        )
        assert v_login.status_code == 200
        v_headers = {"Authorization": f"Bearer {v_login.json()['access_token']}"}
        raw_yaml = Path("examples/acme_retail/client_config.yaml").read_text(encoding="utf-8")
        assert (
            client.post(
                "/api/v1/projects/register-yaml", json={"raw_yaml": raw_yaml}, headers=v_headers
            ).status_code
            == 403
        )

        # Login as admin -> register project & run pipeline
        a_login = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "admin123!"}
        )
        assert a_login.status_code == 200
        a_headers = {"Authorization": f"Bearer {a_login.json()['access_token']}"}

        reg_resp = client.post(
            "/api/v1/projects/register-yaml", json={"raw_yaml": raw_yaml}, headers=a_headers
        )
        assert reg_resp.status_code == 200
        pid = reg_resp.json()["project_id"]

        pipe_resp = client.post(f"/api/v1/projects/{pid}/run-pipeline", headers=a_headers)
        assert pipe_resp.status_code == 200, pipe_resp.text
        assert pipe_resp.json()["reconcile"]["passed"] == 5

        # Engineer cannot perform Client Approver Sign-Off (403)!
        e_login = client.post(
            "/api/v1/auth/login", json={"username": "engineer", "password": "admin123!"}
        )
        e_headers = {"Authorization": f"Bearer {e_login.json()['access_token']}"}
        assert (
            client.post(
                f"/api/v1/projects/{pid}/signoff",
                json={"object_key": "legacy_ddl.dbo.orders"},
                headers=e_headers,
            ).status_code
            == 403
        )

        # Client Approver CAN sign off validated table (200)!
        ap_login = client.post(
            "/api/v1/auth/login", json={"username": "approver", "password": "admin123!"}
        )
        ap_headers = {"Authorization": f"Bearer {ap_login.json()['access_token']}"}
        so_resp = client.post(
            f"/api/v1/projects/{pid}/signoff",
            json={"object_key": "legacy_ddl.dbo.orders"},
            headers=ap_headers,
        )
        assert so_resp.status_code == 200

        # Steering summary & Prometheus metrics endpoints
        assert (
            client.get(f"/api/v1/projects/{pid}/exports/steering", headers=v_headers).status_code
            == 200
        )
        assert "l2c_stage_runs_total" in client.get("/metrics").text


def test_cli_commands_e2e(isolated_env: Path) -> None:
    _setup_duckdb_vault(isolated_env)
    runner = CliRunner()

    res_init = runner.invoke(cli_app, ["init"])
    assert res_init.exit_code == 0

    res_plugins = runner.invoke(cli_app, ["plugins", "--run-golden"])
    assert res_plugins.exit_code == 0
    assert "PASS" in res_plugins.stdout

    res_reg = runner.invoke(cli_app, ["register", "examples/acme_retail/client_config.yaml"])
    assert res_reg.exit_code == 0
    assert "project_id=" in res_reg.stdout
