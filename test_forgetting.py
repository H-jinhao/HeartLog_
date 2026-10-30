"""Memory forgetting, derived-memory visibility, and diary unblocking tests."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models.audit import AuditEvent
from app.models.memory import Memory, MemorySource
from app.models.person import Person


def _add_memory(
    session,
    *,
    entry_id: str,
    revision_id: str,
    content: str,
    evidence: str,
    memory_type: str = "explicit_fact",
    verification_status: str = "confirmed",
    supersedes_memory_id: str | None = None,
) -> Memory:
    memory = Memory(
        content=content,
        memory_type=memory_type,
        source_type="diary_entry",
        source_entry_id=entry_id,
        source_revision_id=revision_id,
        confidence=0.8,
        verification_status=verification_status,
        is_active=True,
        created_by="model",
        confirmed_by="user" if verification_status == "confirmed" else None,
        supersedes_memory_id=supersedes_memory_id,
    )
    session.add(memory)
    session.flush()
    session.add(
        MemorySource(
            memory_id=memory.id,
            entry_id=entry_id,
            revision_id=revision_id,
            evidence_text=evidence,
        )
    )
    session.commit()
    session.refresh(memory)
    return memory


def _create_diary(client: TestClient, content: str) -> dict:
    response = client.post("/api/v1/diaries", json={"content": content})
    assert response.status_code == 201, response.text
    return response.json()


def _admin_permanent_headers(diary_id: str) -> dict[str, str]:
    return {
        "X-Admin-Token": "test-admin-token",
        "X-Confirm-Permanent-Delete": diary_id,
    }


def test_purge_memory_then_unblock_diary_permanent_deletion(client: TestClient, db_session) -> None:
    diary = _create_diary(client, "今天我和小林去了公园，我很开心。")
    memory = _add_memory(
        db_session,
        entry_id=diary["id"],
        revision_id=diary["current_revision_id"],
        content="用户今天去了公园",
        evidence="去了公园",
    )
    memory_id = memory.id
    person = client.post(
        "/api/v1/persons",
        json={
            "display_name": "小林",
            "source_entry_id": diary["id"],
            "source_revision_id": diary["current_revision_id"],
            "source_evidence": "小林",
        },
    )
    assert person.status_code == 201, person.text
    person_id = person.json()["id"]

    refs = client.get(f"/api/v1/diaries/{diary['id']}/references")
    assert refs.status_code == 200
    body = refs.json()
    assert body == {
        **body,
        "entry_id": diary["id"],
        "is_deleted": False,
        "memories": 1,
        "memory_sources": 1,
        "persons": 1,
        "can_permanently_delete": False,
    }

    no_confirm = client.delete(f"/api/v1/memories/{memory_id}")
    assert no_confirm.status_code == 422
    assert no_confirm.json()["error"]["code"] == "confirmation_required"
    wrong_confirm = client.delete(
        f"/api/v1/memories/{memory_id}",
        headers={"X-Confirm-Memory-Delete": "wrong"},
    )
    assert wrong_confirm.status_code == 422

    purged = client.delete(
        f"/api/v1/memories/{memory_id}",
        headers={"X-Confirm-Memory-Delete": memory_id},
    )
    assert purged.status_code == 200, purged.text
    purge_body = purged.json()
    assert purge_body["mode"] == "purge"
    assert purge_body["purged_sources"] == 1
    assert purge_body["source_entry_id"] == diary["id"]
    assert purge_body["audit_event_id"]

    db_session.rollback()
    assert db_session.get(Memory, memory_id) is None
    db_session.rollback()
    assert (
        db_session.scalar(
            select(func.count()).select_from(MemorySource).where(MemorySource.memory_id == memory_id)
        )
        == 0
    )
    audit = db_session.get(AuditEvent, purge_body["audit_event_id"])
    assert audit is not None
    assert audit.event_type == "memory.purged"
    serialized_audit = json.dumps(audit.details, ensure_ascii=False)
    assert "用户今天去了公园" not in serialized_audit
    assert "去了公园" not in serialized_audit

    # The person is still a reference, so the diary remains blocked.
    refs = client.get(f"/api/v1/diaries/{diary['id']}/references").json()
    assert refs["memories"] == 0
    assert refs["persons"] == 1
    assert refs["can_permanently_delete"] is False
    blocked = client.delete(
        f"/api/v1/diaries/{diary['id']}/permanent",
        headers=_admin_permanent_headers(diary["id"]),
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "entry_has_references"

    purged_person = client.delete(
        f"/api/v1/persons/{person_id}",
        headers={"X-Confirm-Person-Delete": "true"},
    )
    assert purged_person.status_code == 200, purged_person.text
    refs = client.get(f"/api/v1/diaries/{diary['id']}/references").json()
    assert refs["memories"] == 0
    assert refs["memory_sources"] == 0
    assert refs["persons"] == 0
    assert refs["can_permanently_delete"] is True

    deleted = client.delete(
        f"/api/v1/diaries/{diary['id']}/permanent",
        headers=_admin_permanent_headers(diary["id"]),
    )
    assert deleted.status_code == 200, deleted.text
    assert client.get(f"/api/v1/diaries/{diary['id']}").status_code == 404


def test_anonymize_mode_is_audited_without_content(client: TestClient, db_session) -> None:
    diary = _create_diary(client, "一段用于匿名化测试的日记。")
    memory = _add_memory(
        db_session,
        entry_id=diary["id"],
        revision_id=diary["current_revision_id"],
        content="需要匿名化的记忆正文",
        evidence="匿名化测试",
    )
    memory_id = memory.id
    response = client.delete(
        f"/api/v1/memories/{memory_id}?mode=anonymize",
        headers={"X-Confirm-Memory-Delete": "true"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "anonymize"
    assert body["purged_sources"] == 1
    db_session.rollback()
    assert db_session.get(Memory, memory_id) is None
    event = db_session.get(AuditEvent, body["audit_event_id"])
    assert event is not None
    assert event.event_type == "memory.anonymized"
    serialized = json.dumps(event.details, ensure_ascii=False)
    assert "需要匿名化的记忆正文" not in serialized
    assert "匿名化测试" not in serialized


def test_invalid_forget_mode_is_rejected(client: TestClient, db_session) -> None:
    diary = _create_diary(client, "合法模式校验。")
    memory = _add_memory(
        db_session,
        entry_id=diary["id"],
        revision_id=diary["current_revision_id"],
        content="待删除记忆",
        evidence="合法模式",
    )
    memory_id = memory.id
    response = client.delete(
        f"/api/v1/memories/{memory_id}?mode=shred_everything",
        headers={"X-Confirm-Memory-Delete": "true"},
    )
    assert response.status_code == 422
    db_session.rollback()
    assert db_session.get(Memory, memory_id) is not None


def test_soft_deleted_diary_hides_derived_memory_by_default(client: TestClient, db_session) -> None:
    diary = _create_diary(client, "今天发现了一家很好吃的面包店。")
    memory = _add_memory(
        db_session,
        entry_id=diary["id"],
        revision_id=diary["current_revision_id"],
        content="用户喜欢那家面包店",
        evidence="很好吃的面包店",
    )
    memory_id = memory.id
    assert client.delete(f"/api/v1/diaries/{diary['id']}").status_code == 200

    default_list = client.get("/api/v1/memories").json()
    assert default_list["total"] == 0
    hidden_detail = client.get(f"/api/v1/memories/{memory_id}")
    assert hidden_detail.status_code == 404
    assert client.get(f"/api/v1/memories/{memory_id}/sources").status_code == 404

    visible_list = client.get("/api/v1/memories", params={"include_deleted_sources": "true"}).json()
    assert visible_list["total"] == 1
    assert visible_list["items"][0]["id"] == memory.id
    assert visible_list["items"][0]["source_entry_deleted"] is True
    visible_detail = client.get(f"/api/v1/memories/{memory_id}", params={"include_deleted_sources": "true"})
    assert visible_detail.status_code == 200
    assert visible_detail.json()["content"] == "用户喜欢那家面包店"
    visible_sources = client.get(
        f"/api/v1/memories/{memory.id}/sources",
        params={"include_deleted_sources": "true"},
    )
    assert visible_sources.status_code == 200
    assert visible_sources.json()["total"] == 1

    search_default = client.get("/api/v1/search", params={"q": "面包店", "scope": "memories"}).json()
    assert search_default["total"] == 0
    assert search_default["include_deleted_sources"] is False
    search_visible = client.get(
        "/api/v1/search",
        params={
            "q": "面包店",
            "scope": "memories",
            "include_deleted_sources": "true",
        },
    ).json()
    assert search_visible["total"] == 1
    assert search_visible["include_deleted_sources"] is True

    # Targeted purge of a hidden memory still works by id.
    purge = client.delete(
        f"/api/v1/memories/{memory_id}",
        headers={"X-Confirm-Memory-Delete": memory_id},
    )
    assert purge.status_code == 200, purge.text
    refs = client.get(f"/api/v1/diaries/{diary['id']}/references").json()
    assert refs["can_permanently_delete"] is True


def test_soft_deleted_diary_hides_person_by_default(client: TestClient) -> None:
    diary = _create_diary(client, "今天小雨来找我。")
    created = client.post(
        "/api/v1/persons",
        json={
            "display_name": "小雨",
            "source_entry_id": diary["id"],
            "source_revision_id": diary["current_revision_id"],
            "source_evidence": "小雨",
        },
    )
    assert created.status_code == 201
    person_id = created.json()["id"]
    assert client.delete(f"/api/v1/diaries/{diary['id']}").status_code == 200

    assert client.get("/api/v1/persons").json()["total"] == 0
    assert client.get(f"/api/v1/persons/{person_id}").status_code == 404
    visible = client.get("/api/v1/persons", params={"include_deleted_sources": "true"}).json()
    assert visible["total"] == 1
    assert (
        client.get(
            f"/api/v1/persons/{person_id}",
            params={"include_deleted_sources": "true"},
        ).status_code
        == 200
    )
    purged = client.delete(
        f"/api/v1/persons/{person_id}",
        headers={"X-Confirm-Person-Delete": person_id},
    )
    assert purged.status_code == 200
    assert db_count(client)["persons"] == 0


def test_purge_superseded_parent_sets_child_link_null(client: TestClient, db_session) -> None:
    diary = _create_diary(client, "记忆更正链的清理测试：第一版和第二版。")
    parent = _add_memory(
        db_session,
        entry_id=diary["id"],
        revision_id=diary["current_revision_id"],
        content="第一版记忆",
        evidence="第一版",
    )
    child = _add_memory(
        db_session,
        entry_id=diary["id"],
        revision_id=diary["current_revision_id"],
        content="第二版记忆",
        evidence="第二版",
        supersedes_memory_id=parent.id,
    )
    child_id = child.id
    response = client.delete(
        f"/api/v1/memories/{parent.id}",
        headers={"X-Confirm-Memory-Delete": parent.id},
    )
    assert response.status_code == 200, response.text
    db_session.rollback()
    remaining = db_session.get(Memory, child_id)
    assert remaining is not None
    assert remaining.supersedes_memory_id is None


def test_insights_exclude_memories_derived_from_soft_deleted_diaries(client: TestClient, db_session) -> None:
    from datetime import UTC, datetime

    diary = _create_diary(client, "今天心情很平稳。")
    _add_memory(
        db_session,
        entry_id=diary["id"],
        revision_id=diary["current_revision_id"],
        content="用户今天心情平稳",
        evidence="心情很平稳",
    )
    assert client.delete(f"/api/v1/diaries/{diary['id']}").status_code == 200
    reference_date = datetime.now(UTC).date().isoformat()
    insight = client.get("/api/v1/insights/weekly", params={"date": reference_date}).json()
    assert insight["mode"] == "local_statistics"
    assert insight["statistics"]["diary_entries"] == 0
    assert insight["statistics"]["memories"] == 0


def test_person_forget_requires_confirmation(client: TestClient) -> None:
    diary = _create_diary(client, "今天小周来家里吃饭。")
    created = client.post(
        "/api/v1/persons",
        json={
            "display_name": "小周",
            "source_entry_id": diary["id"],
            "source_revision_id": diary["current_revision_id"],
            "source_evidence": "小周",
        },
    )
    assert created.status_code == 201
    person_id = created.json()["id"]
    assert client.delete(f"/api/v1/persons/{person_id}").status_code == 422
    assert (
        client.delete(
            f"/api/v1/persons/{person_id}",
            headers={"X-Confirm-Person-Delete": "wrong"},
        ).status_code
        == 422
    )
    assert (
        client.delete(
            f"/api/v1/persons/{person_id}",
            headers={"X-Confirm-Person-Delete": "true"},
        ).status_code
        == 200
    )
    assert client.get(f"/api/v1/persons/{person_id}").status_code == 404
    assert client.get(f"/api/v1/persons/{person_id}/timeline").status_code == 404


def test_diary_references_404_for_unknown_entry(client: TestClient) -> None:
    assert client.get("/api/v1/diaries/no-such-diary/references").status_code == 404


def db_count(client: TestClient) -> dict[str, int]:
    with client.app.state.session_factory() as session:  # type: ignore[union-attr]
        return {
            "persons": int(session.scalar(select(func.count()).select_from(Person)) or 0),
        }
