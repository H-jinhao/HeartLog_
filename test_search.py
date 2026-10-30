"""Keyword search behavior and source metadata."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_search_returns_source_version_and_snippet(client: TestClient, diary_factory) -> None:
    diary = diary_factory("上周我们去了西湖，那天下着小雨。")
    result = client.get("/api/v1/search", params={"q": "西湖"}).json()
    assert result["total"] == 1
    hit = result["items"][0]
    assert hit["kind"] == "diary"
    assert hit["entry_id"] == diary["id"]
    assert hit["revision_id"] == diary["current_revision_id"]
    assert hit["version"] == 1
    assert "西湖" in hit["snippet"]
    assert hit["is_current"] is True


def test_search_history_versions(client: TestClient, diary_factory) -> None:
    diary = diary_factory("旧版本里出现了蓝色风筝。")
    diary_id = diary["id"]
    client.put(f"/api/v1/diaries/{diary_id}", json={"content": "新版本删掉了这个细节。"})

    current_only = client.get("/api/v1/search", params={"q": "蓝色风筝", "include_history": False}).json()
    assert current_only["total"] == 0

    with_history = client.get("/api/v1/search", params={"q": "蓝色风筝", "include_history": True}).json()
    assert with_history["total"] == 1
    assert with_history["items"][0]["version"] == 1
    assert with_history["items"][0]["is_current"] is False


def test_search_excludes_soft_deleted_diaries(client: TestClient, diary_factory) -> None:
    diary = diary_factory("一次独一无二的旅行记录")
    assert client.delete(f"/api/v1/diaries/{diary['id']}").status_code == 200
    result = client.get("/api/v1/search", params={"q": "独一无二"}).json()
    assert result["total"] == 0


def test_search_validates_query(client: TestClient) -> None:
    assert client.get("/api/v1/search", params={"q": "   "}).status_code == 422
    assert client.get("/api/v1/search").status_code == 422
    assert client.get("/api/v1/search", params={"q": "字" * 201}).status_code == 422
    assert client.get("/api/v1/search", params={"q": "%"}).status_code == 200


def test_search_scope_validation(client: TestClient) -> None:
    assert client.get("/api/v1/search", params={"q": "x", "scope": "invalid"}).status_code == 422
