"""Data export and persistence across app restarts."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_export_contains_original_revisions_and_audit(client: TestClient, diary_factory) -> None:
    diary = diary_factory("导出测试的原始文本")
    diary_id = diary["id"]
    client.put(f"/api/v1/diaries/{diary_id}", json={"content": "导出测试的当前文本"})
    response = client.get("/api/v1/export")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    payload = response.json()
    assert payload["schema_version"] == "heartlog.export.v1"
    assert payload["counts"]["diaries"] == 1
    assert payload["counts"]["revisions"] == 2
    assert payload["counts"]["audit_events"] >= 2
    exported = payload["diaries"][0]
    assert exported["original_text"] == "导出测试的原始文本"
    assert [revision["content"] for revision in exported["revisions"]] == [
        "导出测试的原始文本",
        "导出测试的当前文本",
    ]
    assert "api_key" not in json.dumps(payload).lower()
    assert "admin_token" not in json.dumps(payload).lower()


def test_data_survives_app_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "persistent.db"
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{db_path.as_posix()}",
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "backups",
        api_token=None,
        admin_token="admin",
        _env_file=None,
    )
    app1 = create_app(settings)
    try:
        with TestClient(app1) as client:
            created = client.post("/api/v1/diaries", json={"content": "重启后仍然存在"}).json()
            diary_id = created["id"]
    finally:
        app1.state.engine.dispose()

    app2 = create_app(settings)
    try:
        with TestClient(app2) as client:
            response = client.get(f"/api/v1/diaries/{diary_id}")
            assert response.status_code == 200
            assert response.json()["original_text"] == "重启后仍然存在"
            assert response.json()["current_version"] == 1
    finally:
        app2.state.engine.dispose()
