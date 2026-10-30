"""Diary CRUD, pagination, validation, soft delete and restore."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient


def test_create_diary_saves_exact_original_and_initial_revision(client: TestClient) -> None:
    original = "  她今天没有回复我的消息，我有点失落。\n\n但后来我去散步了。  "
    response = client.post("/api/v1/diaries", json={"content": original, "metadata": {"mood": "低"}})
    assert response.status_code == 201
    body = response.json()
    assert body["original_text"] == original
    assert body["current_content"] == original
    assert body["current_version"] == 1
    assert body["current_revision"]["version"] == 1
    assert body["current_revision"]["content"] == original
    assert body["metadata"] == {"mood": "低"}
    assert len(body["original_content_hash"]) == 64

    revisions = client.get(f"/api/v1/diaries/{body['id']}/revisions")
    assert revisions.status_code == 200
    assert revisions.json()["total"] == 1
    assert revisions.json()["versions"][0]["content"] == original


def test_unknown_diary_returns_404(client: TestClient) -> None:
    assert client.get("/api/v1/diaries/does-not-exist").status_code == 404
    assert client.get("/api/v1/diaries/does-not-exist/revisions").status_code == 404
    assert client.put("/api/v1/diaries/does-not-exist", json={"content": "x"}).status_code == 404
    assert client.delete("/api/v1/diaries/does-not-exist").status_code == 404


def test_invalid_diary_input_returns_4xx(client: TestClient) -> None:
    assert client.post("/api/v1/diaries", json={"content": ""}).status_code == 422
    assert client.post("/api/v1/diaries", json={"content": "   \n  "}).status_code == 422
    assert client.post("/api/v1/diaries", json={}).status_code == 422
    assert client.post("/api/v1/diaries", json={"content": "ok", "unexpected": 1}).status_code == 422
    too_long = "字" * 50_001
    assert client.post("/api/v1/diaries", json={"content": too_long}).status_code == 422


def test_list_pagination_and_keyword_filter(client: TestClient, diary_factory) -> None:
    first = diary_factory("今天和朋友去公园散步，心情很好。")
    second = diary_factory("工作有点累，但完成了报告。")
    third = diary_factory("周末在家看书，想了一些关于未来的事情。")

    page = client.get("/api/v1/diaries", params={"limit": 2, "offset": 0}).json()
    assert page["total"] == 3
    assert len(page["items"]) == 2
    assert page["has_more"] is True
    page2 = client.get("/api/v1/diaries", params={"limit": 2, "offset": 2}).json()
    assert len(page2["items"]) == 1
    assert page2["has_more"] is False

    searched = client.get("/api/v1/diaries", params={"q": "公园"}).json()
    assert searched["total"] == 1
    assert searched["items"][0]["id"] == first["id"]
    assert "公园" in searched["items"][0]["preview"]

    wildcard = client.get("/api/v1/diaries", params={"q": "%"}).json()
    assert wildcard["total"] == 0, "LIKE wildcards in user input must be escaped"
    assert second["id"] != third["id"]


def test_metadata_is_size_validated(client: TestClient) -> None:
    ok = client.post(
        "/api/v1/diaries",
        json={"content": "元数据测试", "metadata": {"tags": ["生活", "关系"], "mood": 3}},
    )
    assert ok.status_code == 201
    too_large = client.post(
        "/api/v1/diaries",
        json={"content": "元数据过大", "metadata": {"blob": "x" * 5_000}},
    )
    assert too_large.status_code == 422
    too_deep = client.post(
        "/api/v1/diaries",
        json={"content": "元数据过深", "metadata": {"a": {"b": {"c": {"d": 1}}}}},
    )
    assert too_deep.status_code == 422
    wrong_key = client.post(
        "/api/v1/diaries",
        json={"content": "元数据键", "metadata": {"k" * 101: 1}},
    )
    assert wrong_key.status_code == 422


def test_date_range_filter(client: TestClient, diary_factory, db_session) -> None:
    diary = diary_factory("日期过滤测试")
    from sqlalchemy import update

    from app.models.diary import DiaryEntry

    old = datetime(2020, 1, 1, tzinfo=UTC)
    db_session.execute(update(DiaryEntry).where(DiaryEntry.id == diary["id"]).values(created_at=old))
    db_session.commit()

    after = (old + timedelta(days=1)).isoformat()
    found = client.get("/api/v1/diaries", params={"date_from": after}).json()
    assert found["total"] == 0
    found2 = client.get(
        "/api/v1/diaries", params={"date_from": "2019-01-01T00:00:00Z", "date_to": after}
    ).json()
    assert found2["total"] == 1


def test_soft_delete_hides_from_list_and_can_restore(client: TestClient, diary_factory, db_session) -> None:
    diary = diary_factory("可恢复的日记")
    diary_id = diary["id"]
    response = client.delete(f"/api/v1/diaries/{diary_id}")
    assert response.status_code == 200
    assert response.json()["id"] == diary_id

    assert client.get("/api/v1/diaries").json()["total"] == 0
    assert client.get(f"/api/v1/diaries/{diary_id}").status_code == 404
    assert client.get(f"/api/v1/diaries/{diary_id}/revisions").status_code == 404

    from app.models.diary import DiaryEntry, DiaryRevision

    entry = db_session.get(DiaryEntry, diary_id)
    assert entry is not None and entry.deleted_at is not None
    revisions = db_session.query(DiaryRevision).filter_by(entry_id=diary_id).all()
    assert len(revisions) == 1, "soft delete must not delete revision history"

    restored = client.post(f"/api/v1/diaries/{diary_id}/restore")
    assert restored.status_code == 200
    assert restored.json()["deleted_at"] is None
    assert client.get("/api/v1/diaries").json()["total"] == 1


def test_soft_delete_is_not_permanent_without_admin(client: TestClient, diary_factory) -> None:
    diary = diary_factory("普通用户不能永久删除")
    diary_id = diary["id"]
    assert client.delete(f"/api/v1/diaries/{diary_id}").status_code == 200
    assert (
        client.delete(
            f"/api/v1/diaries/{diary_id}/permanent",
            headers={"X-Confirm-Permanent-Delete": "true"},
        ).status_code
        == 403
    )
    assert client.post(f"/api/v1/diaries/{diary_id}/restore").status_code == 200


def test_permanent_delete_requires_confirmation_and_admin(client: TestClient, diary_factory) -> None:
    diary = diary_factory("永久删除需要明确确认")
    diary_id = diary["id"]
    admin_headers = {"X-Admin-Token": "test-admin-token"}
    missing_confirm = client.delete(f"/api/v1/diaries/{diary_id}/permanent", headers=admin_headers)
    assert missing_confirm.status_code == 422
    wrong_confirm = client.delete(
        f"/api/v1/diaries/{diary_id}/permanent",
        headers={**admin_headers, "X-Confirm-Permanent-Delete": "no"},
    )
    assert wrong_confirm.status_code == 422
    deleted = client.delete(
        f"/api/v1/diaries/{diary_id}/permanent",
        headers={**admin_headers, "X-Confirm-Permanent-Delete": diary_id},
    )
    assert deleted.status_code == 200
    assert client.get(f"/api/v1/diaries/{diary_id}/revisions").status_code == 404
