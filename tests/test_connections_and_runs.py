"""Stage 6a: saved connections, connection tests, uploads, migrations and live runs via the API."""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient
from test_security import _app, _login

from accelerator.core.secrets import get_vault
from accelerator.db.models import Connection, PipelineRun
from accelerator.db.session import session_scope
from accelerator.orchestration.live_runs import mark_interrupted_runs

ACME = Path("examples/acme_retail")


def _seed(tmp: Path) -> tuple[Path, Path]:
    spec = importlib.util.spec_from_file_location("seed_acme", ACME / "seed_legacy_db.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.seed_all(base_dir=tmp / "acme")


def _wait(c: TestClient, h: dict, pid: str, run_id: str, timeout: float = 120) -> dict:
    deadline = time.time() + timeout
    seen_running = False
    while time.time() < deadline:
        run = c.get(f"/api/v1/projects/{pid}/runs/{run_id}", headers=h).json()
        seen_running |= run["status"] == "running"
        if run["status"] not in ("queued", "running"):
            run["seen_running"] = seen_running
            return run
        time.sleep(0.2)
    raise AssertionError("run did not finish")


def test_connector_catalog_describes_each_form(isolated_env: Path) -> None:
    with _app() as c:
        h = _login(c, "admin")
        types = {t["id"]: t for t in c.get("/api/v1/connector-types", headers=h).json()}
    assert {"oracle", "sqlserver", "teradata", "snowflake", "databricks", "fabric"} <= set(types)
    oracle = {f["name"]: f for f in types["oracle"]["fields"]}
    assert oracle["password"]["secret"] and not oracle["host"]["secret"]
    assert oracle["port"]["default"] == 1521
    assert types["snowflake"]["kind"] == "destination"


def test_secrets_never_leave_the_vault(isolated_env: Path) -> None:
    with _app() as c:
        h = _login(c, "admin")
        c.post("/api/v1/clients", headers=h, json={"name": "Acme Retail"})
        body = {
            "client": "acme_retail",
            "name": "Legacy PG",
            "type": "postgres",
            "settings": {
                "host": "127.0.0.1",
                "port": 1,
                "database": "x",
                "username": "u",
                "password": "Sup3r$ecret",
            },
        }
        created = c.post("/api/v1/connections", headers=h, json=body)
        assert created.status_code == 200, created.text
        conn = created.json()
        assert "Sup3r$ecret" not in json.dumps(conn)
        assert conn["secret_fields_set"] == ["password"]
        with session_scope() as db:
            row = db.get(Connection, conn["id"])
            assert row is not None and "Sup3r$ecret" not in json.dumps(row.properties)
            assert "Sup3r%24ecret" in get_vault().get_secret(row.secret_ref)  # URL-encoded
        # A failing test reports a readable reason and never echoes the password.
        r = c.post(f"/api/v1/connections/{conn['id']}/test", headers=h).json()
        assert r["ok"] is False and "Sup3r" not in r["message"]
        listed = c.get("/api/v1/connections?client=acme_retail", headers=h).json()
        assert listed[0]["last_test_status"] == "failed"
        # Editing without retyping the password keeps the stored one.
        c.put(f"/api/v1/connections/{conn['id']}", headers=h, json={"settings": {"port": 2}})
        with session_scope() as db:
            row = db.get(Connection, conn["id"])
            assert row is not None and ":2/x" in get_vault().get_secret(row.secret_ref)
            assert "Sup3r%24ecret" in get_vault().get_secret(row.secret_ref)


def test_build_and_run_a_migration_from_the_api(isolated_env: Path) -> None:
    src, tgt = _seed(isolated_env)
    with _app() as c:
        h = _login(c, "admin")
        assert c.post("/api/v1/clients", headers=h, json={"name": "Acme Retail"}).status_code == 200

        def make(name: str, path: Path, kind: str) -> str:
            settings = {"path": str(path), "acts_as": "databricks"}
            test = c.post(
                "/api/v1/connections/test", headers=h, json={"type": "duckdb", "settings": settings}
            ).json()
            assert test["ok"], test
            r = c.post(
                "/api/v1/connections",
                headers=h,
                json={
                    "client": "acme_retail",
                    "name": name,
                    "type": "duckdb",
                    "kind": kind,
                    "settings": settings,
                },
            )
            assert r.status_code == 200, r.text
            return r.json()["id"]

        src_id, dst_id = make("Legacy SQL Server", src, "source"), make("Cloud", tgt, "destination")
        assert "dbo" in c.get(f"/api/v1/connections/{src_id}/schemas", headers=h).json()["schemas"]

        uploads = []
        for f in (ACME / "ssis" / "Load_Customers_And_Orders.dtsx", ACME / "legacy_ddl.sql"):
            r = c.post(
                "/api/v1/clients/acme_retail/uploads",
                headers=h,
                files={"file": (f.name, f.read_bytes())},
            )
            assert r.status_code == 200, r.text
            uploads.append(r.json())
        assert uploads[0]["tool"] == "ssis"

        m = c.post(
            "/api/v1/migrations",
            headers=h,
            json={
                "client": "acme_retail",
                "name": "Acme to Databricks",
                "source_connection_id": src_id,
                "destination_connection_id": dst_id,
                "catalog_file": uploads[1]["path"],
                "etl_files": [uploads[0]["path"]],
                "chunk_rows": 50,
            },
        )
        assert m.status_code == 200, m.text
        pid = m.json()["id"]
        assert "platform: databricks" in m.json()["config_yaml"]
        assert "vault://acme_retail/connections/" in m.json()["config_yaml"]

        run = c.post(f"/api/v1/projects/{pid}/runs", headers=h, json={}).json()
        assert run["status"] in ("queued", "running")
        done = _wait(c, h, pid, run["id"])
        assert done["status"] == "succeeded", done
        assert [s["status"] for s in done["stages"]] == ["done"] * 5
        move = next(s for s in done["stages"] if s["name"] == "move")
        assert move["summary"]["tables_loaded"] == 5
        recon = next(s for s in done["stages"] if s["name"] == "reconcile")
        assert recon["summary"]["passed"] == 5 and recon["summary"]["failed"] == 0, str(
            recon["summary"]
        )

        summary = c.get(f"/api/v1/migrations/{pid}", headers=h).json()
        assert summary["source"]["name"] == "Legacy SQL Server"
        assert summary["destination"]["name"] == "Cloud"
        assert summary["last_run"]["id"] == run["id"]
        assert summary["by_state"].get("Validated") == 5
        # A connection in use by a migration can't be deleted.
        assert c.delete(f"/api/v1/connections/{src_id}", headers=h).status_code == 400


def test_one_run_at_a_time_and_failures_are_recorded(isolated_env: Path) -> None:
    with _app() as c:
        h = _login(c, "admin")
        c.post("/api/v1/clients", headers=h, json={"name": "Acme Retail"})
        src, tgt = _seed(isolated_env)
        ids = []
        for name, path, kind in (("S", src, "source"), ("T", tgt, "destination")):
            ids.append(
                c.post(
                    "/api/v1/connections",
                    headers=h,
                    json={
                        "client": "acme_retail",
                        "name": name,
                        "type": "duckdb",
                        "kind": kind,
                        "settings": {"path": str(path)},
                    },
                ).json()["id"]
            )
        pid = c.post(
            "/api/v1/migrations",
            headers=h,
            json={
                "client": "acme_retail",
                "name": "M",
                "source_connection_id": ids[0],
                "destination_connection_id": ids[1],
            },
        ).json()["id"]
        with session_scope() as db:
            db.add(PipelineRun(project_id=pid, status="running", stages=[], created_by="x"))
        busy = c.post(f"/api/v1/projects/{pid}/runs", headers=h, json={})
        assert busy.status_code == 400 and "already in progress" in busy.json()["detail"]
        assert mark_interrupted_runs() == 1  # what start-up does after a restart
        # A stage that fails stops the run there; later stages are skipped, not run.
        broken = c.post(
            "/api/v1/connections",
            headers=h,
            json={
                "client": "acme_retail",
                "name": "Missing",
                "type": "duckdb",
                "kind": "source",
                "settings": {"path": str(isolated_env / "no_such_dir" / "missing.duckdb")},
            },
        ).json()
        pid2 = c.post(
            "/api/v1/migrations",
            headers=h,
            json={
                "client": "acme_retail",
                "name": "Broken",
                "source_connection_id": broken["id"],
                "destination_connection_id": ids[1],
            },
        ).json()["id"]
        run = c.post(f"/api/v1/projects/{pid2}/runs", headers=h, json={}).json()
        done = _wait(c, h, pid2, run["id"])
        assert done["status"] == "failed", done
        statuses = [s["status"] for s in done["stages"]]
        assert statuses == ["failed", "skipped", "skipped", "skipped", "skipped"]
        assert done["error"].startswith("discover:")


def test_clients_and_connections_are_private(isolated_env: Path) -> None:
    with _app() as c:
        admin = _login(c, "admin")
        c.post(
            "/api/v1/auth/users",
            headers=admin,
            json={
                "username": "eve",
                "email": "eve@x.test",
                "role_name": "migration_lead",
                "password": "S3cure-pass!",
            },
        )
        c.post("/api/v1/clients", headers=admin, json={"name": "Acme Retail"})
        conn = c.post(
            "/api/v1/connections",
            headers=admin,
            json={
                "client": "acme_retail",
                "name": "S",
                "type": "duckdb",
                "kind": "source",
                "settings": {"path": "x.duckdb"},
            },
        ).json()
        eve = _login(c, "eve", "S3cure-pass!")
        assert c.get("/api/v1/clients", headers=eve).json() == []
        assert c.get("/api/v1/connections", headers=eve).json() == []
        assert c.post(f"/api/v1/connections/{conn['id']}/test", headers=eve).status_code == 404
        # eve's own client is hers
        assert c.post("/api/v1/clients", headers=eve, json={"name": "Eve Co"}).status_code == 200
        assert [x["slug"] for x in c.get("/api/v1/clients", headers=eve).json()] == ["eve_co"]
