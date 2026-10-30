"""Health and basic app-contract tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["ai_enabled"] is False
    assert body["ai_ready"] is False
    assert body["app"] == "HeartLog"


def test_root_and_openapi_available(client: TestClient) -> None:
    assert client.get("/").status_code == 200
    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    paths = openapi.json()["paths"]
    assert "/api/v1/diaries" in paths
    assert "/api/v1/memories" in paths
    assert "/api/v1/search" in paths


def test_response_has_privacy_headers_and_sanitised_request_id(client: TestClient) -> None:
    first = client.get("/health", headers={"X-Request-ID": "safe-trace-123"})
    assert first.headers["X-Request-ID"] == "safe-trace-123"
    assert first.headers["X-Content-Type-Options"] == "nosniff"
    unsafe = "x" * 100
    second = client.get("/health", headers={"X-Request-ID": unsafe})
    assert second.headers["X-Request-ID"] != unsafe
    api = client.get("/api/v1/diaries")
    assert api.headers["Cache-Control"] == "no-store"


def test_health_is_public_when_api_token_configured(tmp_path) -> None:
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{(tmp_path / 'auth.db').as_posix()}",
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "backups",
        api_token="secret-token",
        admin_token="admin-token",
        _env_file=None,
    )
    app = create_app(settings)
    try:
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/api/v1/diaries").status_code == 401
            assert client.get("/api/v1/diaries", headers={"Authorization": "Bearer wrong"}).status_code == 401
            assert (
                client.get("/api/v1/diaries", headers={"Authorization": "Bearer secret-token"}).status_code
                == 200
            )
            assert (
                client.get("/api/v1/export", headers={"Authorization": "Bearer secret-token"}).status_code
                == 200
            )
            assert client.get("/api/v1/export").status_code == 401
    finally:
        app.state.engine.dispose()


def test_admin_operations_disabled_when_no_admin_token(tmp_path) -> None:
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{(tmp_path / 'noadmin.db').as_posix()}",
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "backups",
        admin_token=None,
        _env_file=None,
    )
    app = create_app(settings)
    try:
        with TestClient(app) as client:
            created = client.post("/api/v1/diaries", json={"content": "今天很平静。"})
            assert created.status_code == 201
            diary_id = created.json()["id"]
            assert (
                client.delete(
                    f"/api/v1/diaries/{diary_id}/permanent",
                    headers={"X-Confirm-Permanent-Delete": "true"},
                ).status_code
                == 403
            )
    finally:
        app.state.engine.dispose()


def test_blank_optional_secrets_are_treated_as_unset(tmp_path) -> None:
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{(tmp_path / 'blank.db').as_posix()}",
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "backups",
        api_token="",
        admin_token="",
        ai_api_key="",
        _env_file=None,
    )
    assert settings.api_token is None
    assert settings.admin_token is None
    assert settings.ai_api_key is None
    assert settings.ai_configured is False
