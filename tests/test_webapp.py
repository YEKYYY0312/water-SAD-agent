"""Step 36 Web application skeleton contracts."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from water_agent.webapp.app import create_app


def _client(tmp_path: Path) -> TestClient:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('workbench');", encoding="utf-8")
    (tmp_path / "index.html").write_text(
        '<main>给排水 Agent 工作台</main><script src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    return TestClient(create_app(static_dir=tmp_path))


def test_live_health_reports_the_versioned_service_contract(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {
        "version": "v1",
        "ok": True,
        "data": {"status": "live"},
    }


def test_ready_health_is_explicit_about_step_36_placeholders(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/health/ready")

    assert response.status_code == 200
    assert response.json()["version"] == "v1"
    assert response.json()["ok"] is True
    assert response.json()["data"] == {
        "status": "ready",
        "checks": {
            "database": "not_required_until_step_38",
            "migrations": "placeholder_until_step_38",
        },
    }


def test_unknown_route_uses_the_versioned_error_envelope(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/not-a-route")

    assert response.status_code == 404
    assert response.json() == {
        "version": "v1",
        "ok": False,
        "error": {"code": "WEB_NOT_FOUND", "message": "Resource not found"},
    }


def test_static_workbench_is_served_from_the_fastapi_application(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.get("/")

    assert response.status_code == 200
    assert "给排水 Agent 工作台" in response.text
    assert 'src="/assets/' in response.text
    asset = client.get("/assets/app.js")
    assert asset.status_code == 200
    assert "workbench" in asset.text


def test_capabilities_do_not_claim_a_real_llm_without_a_configured_key(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY_FILE", raising=False)

    response = _client(tmp_path).get("/api/system/capabilities")

    assert response.status_code == 200
    assert response.json()["data"]["real_llm"] is False
    assert response.json()["data"]["authentication"] == "not_implemented_until_step_37"


def test_capabilities_do_not_treat_a_missing_secret_file_as_a_configured_model(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(tmp_path / "missing-secret-file"))

    response = _client(tmp_path).get("/api/system/capabilities")

    assert response.status_code == 200
    assert response.json()["data"]["real_llm"] is False
