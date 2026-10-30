"""Revision history, optimistic concurrency, rollback and immutability."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from app.core.exceptions import ConflictError, ProtectedRecordError
from app.models.audit import AuditEvent
from app.models.diary import DiaryEntry, DiaryRevision
from app.services.diary_service import DiaryService


def test_update_appends_revision_without_touching_original(client: TestClient, diary_factory) -> None:
    original = "第一版原文。"
    diary = diary_factory(original)
    diary_id = diary["id"]
    updated = client.put(
        f"/api/v1/diaries/{diary_id}",
        json={"content": "第二版内容。", "expected_version": 1, "change_reason": "补充细节"},
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["original_text"] == original
    assert body["current_content"] == "第二版内容。"
    assert body["current_version"] == 2
    assert body["current_revision"]["version"] == 2
    assert body["current_revision"]["previous_revision_hash"] == body["original_content_hash"]
    assert body["current_revision"]["change_reason"] == "补充细节"

    revisions = client.get(f"/api/v1/diaries/{diary_id}/revisions").json()
    assert [v["version"] for v in revisions["versions"]] == [1, 2]
    assert revisions["versions"][0]["content"] == original
    assert revisions["versions"][1]["content"] == "第二版内容。"
    assert revisions["versions"][0]["content"] != revisions["versions"][1]["content"]

    detail = client.get(f"/api/v1/diaries/{diary_id}").json()
    assert detail["original_text"] == original
    assert detail["current_content"] == "第二版内容。"


def test_stale_expected_version_returns_409(client: TestClient, diary_factory) -> None:
    diary = diary_factory("并发编辑保护")
    diary_id = diary["id"]
    assert (
        client.put(
            f"/api/v1/diaries/{diary_id}",
            json={"content": "第一次更新", "expected_version": 1},
        ).status_code
        == 200
    )
    stale = client.put(
        f"/api/v1/diaries/{diary_id}",
        json={"content": "基于旧版本的更新", "expected_version": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "version_conflict"


def test_two_sessions_do_not_silently_overwrite(client: TestClient, app) -> None:
    with TestClient(app) as c:
        created = c.post("/api/v1/diaries", json={"content": "并发开始"}).json()
    diary_id = created["id"]
    factory = app.state.session_factory
    session_a = factory()
    session_b = factory()
    try:
        service_a = DiaryService(session_a, app.state.settings)
        service_b = DiaryService(session_b, app.state.settings)
        result = service_a.update(
            diary_id,
            content="会话A写入",
            expected_version=1,
            change_reason="A",
        )
        assert result.current_version == 2
        with pytest.raises(ConflictError):
            service_b.update(
                diary_id,
                content="会话B写入",
                expected_version=1,
                change_reason="B",
            )
    finally:
        session_a.close()
        session_b.close()
    check = factory()
    try:
        entry = check.get(DiaryEntry, diary_id)
        assert entry is not None
        assert entry.current_version == 2
        revisions = check.query(DiaryRevision).filter_by(entry_id=diary_id).all()
        assert len(revisions) == 2
        assert {revision.content for revision in revisions} == {"并发开始", "会话A写入"}
    finally:
        check.close()


def test_failed_revision_write_rolls_back_entirely(client: TestClient, app, monkeypatch) -> None:
    created = client.post("/api/v1/diaries", json={"content": "事务回滚原文"}).json()
    diary_id = created["id"]
    session = app.state.session_factory()
    try:
        service = DiaryService(session, app.state.settings)
        original_audit = service._audit

        def failing_audit(*args, **kwargs):
            original_audit(*args, **kwargs)
            raise RuntimeError("simulated audit failure after writes")

        monkeypatch.setattr(service, "_audit", failing_audit)
        with pytest.raises(RuntimeError):
            service.update(
                diary_id,
                content="不应落库的新版本",
                expected_version=1,
                change_reason=None,
            )
    finally:
        session.close()
    fresh = app.state.session_factory()
    try:
        entry = fresh.get(DiaryEntry, diary_id)
        assert entry is not None
        assert entry.current_version == 1
        assert entry.current_revision_id == created["current_revision_id"]
        revisions = fresh.query(DiaryRevision).filter_by(entry_id=diary_id).all()
        assert len(revisions) == 1
        assert revisions[0].content == "事务回滚原文"
    finally:
        fresh.close()


def test_orm_blocks_original_text_update(client: TestClient, db_session) -> None:
    created = client.post("/api/v1/diaries", json={"content": "不可变原文"}).json()
    entry = db_session.get(DiaryEntry, created["id"])
    assert entry is not None
    entry.original_text = "尝试篡改"
    with pytest.raises(ProtectedRecordError):
        db_session.commit()
    db_session.rollback()
    fresh = db_session.get(DiaryEntry, created["id"])
    assert fresh is not None
    assert fresh.original_text == "不可变原文"


def test_sql_trigger_blocks_original_text_update(client: TestClient, app) -> None:
    created = client.post("/api/v1/diaries", json={"content": "触发器保护原文"}).json()
    engine = app.state.engine
    with pytest.raises(DatabaseError):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE diary_entries SET original_text = 'raw sql tamper' WHERE id = :id"),
                {"id": created["id"]},
            )
    verify = app.state.session_factory()
    try:
        entry = verify.get(DiaryEntry, created["id"])
        assert entry is not None
        assert entry.original_text == "触发器保护原文"
    finally:
        verify.close()


def test_orm_and_sql_block_revision_update(client: TestClient, app, db_session) -> None:
    created = client.post("/api/v1/diaries", json={"content": "不可变修订"}).json()
    revision_id = created["current_revision_id"]
    revision = db_session.get(DiaryRevision, revision_id)
    assert revision is not None
    revision.content = "篡改修订"
    with pytest.raises(ProtectedRecordError):
        db_session.commit()
    db_session.rollback()

    with pytest.raises(DatabaseError):
        with app.state.engine.begin() as connection:
            connection.execute(
                text("UPDATE diary_revisions SET content = 'raw tamper' WHERE id = :id"),
                {"id": revision_id},
            )


def test_audit_events_are_append_only(client: TestClient, app, db_session) -> None:
    created = client.post("/api/v1/diaries", json={"content": "审计测试"}).json()
    event = db_session.query(AuditEvent).filter_by(entity_id=created["id"]).first()
    assert event is not None
    event.event_type = "tampered"
    with pytest.raises(ProtectedRecordError):
        db_session.commit()
    db_session.rollback()

    with pytest.raises(DatabaseError):
        with app.state.engine.begin() as connection:
            connection.execute(text("DELETE FROM audit_events WHERE id = :id"), {"id": event.id})
    verify = db_session.get(AuditEvent, event.id)
    assert verify is not None, "audit event must survive attempted deletion"
    assert verify.event_type == "diary.created"


def test_integrity_report_detects_tampering(client: TestClient, app) -> None:
    created = client.post("/api/v1/diaries", json={"content": "完整性检查 v1"}).json()
    diary_id = created["id"]
    r = client.put(f"/api/v1/diaries/{diary_id}", json={"content": "完整性检查 v2"})
    assert r.status_code == 200
    report = client.get(f"/api/v1/diaries/{diary_id}/integrity").json()
    assert report["original_text_matches_hash"] is True
    assert report["revision_chain_valid"] is True
    assert report["revisions_checked"] == 2

    # Deliberately remove the database guard in this isolated test DB, simulate
    # a direct tamper, then prove the integrity endpoint notices.
    with app.state.engine.begin() as connection:
        connection.execute(text("DROP TRIGGER trg_diary_revisions_immutable"))
        connection.execute(text("DROP TRIGGER trg_diary_revision_identity_immutable"))
        connection.execute(
            text("UPDATE diary_revisions SET content = 'tampered' WHERE entry_id = :id AND version = 2"),
            {"id": diary_id},
        )
    damaged = client.get(f"/api/v1/diaries/{diary_id}/integrity").json()
    assert damaged["revision_chain_valid"] is False
    assert any(issue["code"] == "revision_hash_mismatch" for issue in damaged["issues"])
