"""The React app is served by the API process, including deep links."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def web_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>L2C app</html>")
    (tmp_path / "assets" / "app.js").write_text("console.log('hi')")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    (tmp_path.parent / "secret.txt").write_text("nope")
    monkeypatch.setenv("L2C_UI_DIR", str(tmp_path))
    return tmp_path


def test_serves_app_assets_and_deep_links(web_dir: Path) -> None:
    from accelerator.api.app import create_app

    client = TestClient(create_app())
    assert "L2C app" in client.get("/").text
    assert "L2C app" in client.get("/migrations/abc-123").text  # refresh on a deep link
    assert client.get("/assets/app.js").text == "console.log('hi')"
    assert client.get("/favicon.svg").text == "<svg/>"
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/api/v1/nope").status_code == 404  # API typos are not swallowed
    assert "nope" not in client.get("/../secret.txt").text


def test_api_only_when_not_built(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("L2C_UI_DIR", "")
    from accelerator.api.app import create_app

    assert TestClient(create_app()).get("/migrations").status_code == 404
