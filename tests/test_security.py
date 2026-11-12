"""Stage 4: each protection is attacked directly and must hold."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from accelerator.config.settings import get_settings

CONFIG = (
    "client: {client}\nproject: {project}\nsource:\n  database: sqlserver\n"
    "  catalog_bundle_path: examples/acme_retail/legacy_ddl.sql\ntarget:\n  platform: snowflake\n"
)


def _login(client: TestClient, user: str, pwd: str = "admin123!") -> dict[str, str]:
    r = client.post("/api/v1/auth/login", json={"username": user, "password": pwd})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _app() -> TestClient:
    from accelerator.api.app import create_app

    return TestClient(create_app())


def test_users_only_see_their_own_clients_projects(isolated_env: Path) -> None:
    with _app() as c:
        admin = _login(c, "admin")
        for name, role in (("alice", "migration_lead"), ("bob", "engineer")):
            r = c.post(
                "/api/v1/auth/users",
                headers=admin,
                json={
                    "username": name,
                    "email": f"{name}@x.test",
                    "role_name": role,
                    "password": "S3cure-pass!",
                },
            )
            assert r.status_code == 200
        pa = c.post(
            "/api/v1/projects/register-yaml",
            headers=admin,
            json={"raw_yaml": CONFIG.format(client="client_a", project="dw")},
        ).json()["project_id"]
        pb = c.post(
            "/api/v1/projects/register-yaml",
            headers=admin,
            json={"raw_yaml": CONFIG.format(client="client_b", project="dw")},
        ).json()["project_id"]
        c.post(
            f"/api/v1/projects/{pa}/members",
            headers=admin,
            json={"username": "alice", "role_name": "migration_lead"},
        )
        c.post(
            f"/api/v1/projects/{pb}/members",
            headers=admin,
            json={"username": "bob", "role_name": "viewer"},
        )
        alice, bob = _login(c, "alice", "S3cure-pass!"), _login(c, "bob", "S3cure-pass!")

        assert [p["id"] for p in c.get("/api/v1/projects", headers=alice).json()] == [pa]
        # Another client's project looks like it doesn't exist, for reads and actions alike.
        assert c.get(f"/api/v1/projects/{pb}", headers=alice).status_code == 404
        assert c.get(f"/api/v1/projects/{pb}/dashboard", headers=alice).status_code == 404
        assert c.post(f"/api/v1/projects/{pb}/discover", headers=alice).status_code == 404
        assert c.get(f"/api/v1/projects/{pa}/inventory", headers=bob).status_code == 404
        # Project role decides what a member may do: bob is only a viewer on client B.
        assert c.get(f"/api/v1/projects/{pb}/dashboard", headers=bob).status_code == 200
        assert c.post(f"/api/v1/projects/{pb}/convert", headers=bob).status_code == 403
        # Re-registering client B's config by name must not let alice take it over.
        r = c.post(
            "/api/v1/projects/register-yaml",
            headers=alice,
            json={"raw_yaml": CONFIG.format(client="client_b", project="dw")},
        )
        assert r.status_code == 403
        # A project alice creates is hers; admin stays a platform role.
        pc = c.post(
            "/api/v1/projects/register-yaml",
            headers=alice,
            json={"raw_yaml": CONFIG.format(client="client_c", project="dw")},
        ).json()["project_id"]
        assert {p["id"] for p in c.get("/api/v1/projects", headers=alice).json()} == {pa, pc}
        assert (
            c.post(
                f"/api/v1/projects/{pc}/members",
                headers=alice,
                json={"username": "bob", "role_name": "admin"},
            ).status_code
            == 403
        )
        # Only admins may write secrets.
        assert (
            c.post(
                "/api/v1/projects/secrets",
                headers=alice,
                json={"reference": "vault://client_b/db", "value": "x"},
            ).status_code
            == 403
        )


def test_uploads_cannot_escape_the_upload_folder(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("L2C_UPLOAD_DIR", str(isolated_env / "uploads"))
    monkeypatch.setenv("L2C_ALLOWED_IMPORT_ROOTS", json.dumps(["examples"]))
    get_settings.cache_clear()
    with _app() as c:
        admin = _login(c, "admin")
        pid = c.post(
            "/api/v1/projects/register-yaml",
            headers=admin,
            json={"raw_yaml": CONFIG.format(client="u", project="p")},
        ).json()["project_id"]
        pkg = Path("examples/acme_retail/ssis/Load_Finance_GL.dtsx").read_bytes()
        outside = isolated_env / "escaped.dtsx"
        for evil in ("../../escaped.dtsx", str(outside), "..\\..\\escaped.dtsx"):
            r = c.post(
                f"/api/v1/projects/{pid}/discover/upload",
                headers=admin,
                files={"file": (evil, pkg, "application/xml")},
            )
            assert r.status_code in (200, 400), r.text
            assert not outside.exists()
        saved = list((isolated_env / "uploads").rglob("*.dtsx"))
        assert saved and all((isolated_env / "uploads" / pid) in p.parents for p in saved)
        r = c.post(
            f"/api/v1/projects/{pid}/discover/upload",
            headers=admin,
            files={"file": ("tool.exe", b"MZ", "application/octet-stream")},
        )
        assert r.status_code == 400
        # Discover may only read server folders on the allow-list.
        r = c.post(
            f"/api/v1/projects/{pid}/discover", headers=admin, json={"extra_etl_paths": ["/etc"]}
        )
        assert r.status_code == 400 and "outside the allowed" in r.json()["detail"]
        # ...and so may client configs registered over the API.
        sneaky = CONFIG.format(client="u", project="p2").replace(
            "examples/acme_retail/legacy_ddl.sql", "/etc/passwd"
        )
        r = c.post("/api/v1/projects/register-yaml", headers=admin, json={"raw_yaml": sneaky})
        assert r.status_code == 400
        # A direct database URI could point at this product's own metadata (every client's data).
        meta_db = CONFIG.format(client="u", project="p3").replace(
            "  catalog_bundle_path: examples/acme_retail/legacy_ddl.sql\n",
            "  connection: sqlite:///.l2c_data/accelerator.db\n",
        )
        r = c.post("/api/v1/projects/register-yaml", headers=admin, json={"raw_yaml": meta_db})
        assert r.status_code == 400 and "vault://" in r.json()["detail"]


def test_production_refuses_unsafe_settings_and_has_no_demo_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from accelerator.api.app import create_app
    from accelerator.db.models import User
    from accelerator.db.session import init_db, session_scope

    url = f"sqlite:///{(tmp_path / 'prod.db').as_posix()}"
    monkeypatch.setenv("L2C_DATABASE_URL", url)
    monkeypatch.setenv("L2C_ENVIRONMENT", "prod")
    monkeypatch.delenv("L2C_JWT_SECRET", raising=False)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        create_app()
    monkeypatch.setenv("L2C_JWT_SECRET", "x" * 40)
    monkeypatch.setenv("L2C_CORS_ALLOWED_ORIGINS", '["*"]')
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="CORS"):
        create_app()
    monkeypatch.setenv("L2C_CORS_ALLOWED_ORIGINS", '["https://l2c.example.com"]')
    get_settings.cache_clear()
    create_app()

    init_db(url)
    with session_scope(url) as db:
        assert db.query(User).count() == 0  # no admin123! accounts outside dev
    monkeypatch.setenv("L2C_BOOTSTRAP_ADMIN_PASSWORD", "Very-Long-Bootstrap-Pass-1")
    get_settings.cache_clear()
    init_db(url)
    with session_scope(url) as db:
        assert [u.username for u in db.query(User).all()] == ["admin"]
    get_settings.cache_clear()


def test_cors_allows_only_configured_origins(isolated_env: Path) -> None:
    with _app() as c:
        pre = {
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        }
        ok = c.options("/api/v1/projects", headers={"Origin": "http://localhost:5173", **pre})
        bad = c.options("/api/v1/projects", headers={"Origin": "https://evil.example", **pre})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in bad.headers


def test_single_sign_on(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer, audience = "https://login.example.com/tenant/v2.0", "l2c-app"
    monkeypatch.setenv("L2C_AUTH_MODE", "oidc")
    monkeypatch.setenv("L2C_OIDC_ISSUER", issuer)
    monkeypatch.setenv("L2C_OIDC_AUDIENCE", audience)
    monkeypatch.setenv("L2C_OIDC_JWKS_URL", "https://login.example.com/keys")
    monkeypatch.setenv("L2C_OIDC_ROLE_MAP", json.dumps({"L2C.Lead": "migration_lead"}))
    get_settings.cache_clear()

    class _Key:
        def __init__(self, k):  # type: ignore[no-untyped-def]
            self.key = k

    class _Jwks:
        def get_signing_key_from_jwt(self, _token):  # type: ignore[no-untyped-def]
            return _Key(key.public_key())

    import accelerator.core.oidc as oidc

    monkeypatch.setattr(oidc, "jwks_client", lambda _url: _Jwks())

    def token(**over):  # type: ignore[no-untyped-def]
        claims = {
            "iss": issuer,
            "aud": audience,
            "exp": int(time.time()) + 300,
            "sub": "u1",
            "preferred_username": "carol",
            "email": "carol@client.test",
            "roles": ["L2C.Lead"],
            **over,
        }
        return {"Authorization": "Bearer " + jwt.encode(claims, key, algorithm="RS256")}

    with _app() as c:
        me = c.get("/api/v1/auth/me", headers=token())
        assert me.status_code == 200 and me.json()["role"] == "migration_lead"
        assert c.get("/api/v1/auth/me", headers=token(aud="other-app")).status_code == 401
        assert (
            c.get("/api/v1/auth/me", headers=token(iss="https://evil.example")).status_code == 401
        )
        assert c.get("/api/v1/auth/me", headers=token(exp=int(time.time()) - 10)).status_code == 401
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        forged = jwt.encode(
            {
                "iss": issuer,
                "aud": audience,
                "exp": int(time.time()) + 300,
                "sub": "x",
                "preferred_username": "mallory",
                "roles": ["L2C.Lead"],
            },
            other_key,
            algorithm="RS256",
        )
        assert (
            c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code
            == 401
        )
        # Unmapped roles fall back to the least privilege.
        assert (
            c.get(
                "/api/v1/auth/me",
                headers=token(
                    roles=["Something.Else"], preferred_username="dave", email="dave@client.test"
                ),
            ).json()["role"]
            == "viewer"
        )
        # Passwords don't work in SSO mode, not even the dev admin's.
        assert (
            c.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "admin123!"}
            ).status_code
            == 401
        )
    get_settings.cache_clear()


def test_expired_samples_are_purged(isolated_env: Path) -> None:
    from accelerator.core.storage import get_artifact_store
    from accelerator.db.base import utcnow
    from accelerator.db.models import Client, Project, ReconResult, ReconRun
    from accelerator.db.session import session_scope
    from accelerator.engines.reconcile import purge_expired_samples

    store = get_artifact_store()
    diff = {
        "l4_diff": {
            "value_mismatch_count": 1,
            "value_mismatch_sample": [{"key": 3, "source": {"email": "cu***le"}}],
        }
    }
    uri = store.put_text("projects/p/evidence/reconcile/r.json", json.dumps({"diff_summary": diff}))
    with session_scope() as db:
        cl = Client(slug="c", name="c")
        db.add(cl)
        db.flush()
        pr = Project(client_id=cl.id, slug="p", name="p", source_platform="sqlserver")
        db.add(pr)
        db.flush()
        run = ReconRun(project_id=pr.id)
        db.add(run)
        db.flush()
        db.add(
            ReconResult(
                recon_run_id=run.id,
                project_id=pr.id,
                object_key="t",
                level="L4",
                verdict="fail",
                diff_summary=diff,
                evidence_uri=uri,
                retention_expires_at=utcnow() - timedelta(days=1),
            )
        )
    with session_scope() as db:
        assert purge_expired_samples(db, store) == 1
        res = db.query(ReconResult).one()
        assert "value_mismatch_sample" not in res.diff_summary["l4_diff"]
        assert res.diff_summary["l4_diff"]["value_mismatch_count"] == 1  # counts stay as evidence
    assert "value_mismatch_sample" not in store.get_text(uri)
