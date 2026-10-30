"""Cross-cutting fact-integrity and source-traceability tests."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError, IntegrityError

from app.models.diary import DiaryEntry, DiaryRevision
from app.models.memory import Memory, MemorySource


def _add_memory(
    session,
    *,
    entry_id: str,
    revision_id: str,
    content: str,
    memory_type: str = "model_inference",
    evidence: str | None = None,
    status: str = "pending",
) -> Memory:
    entry = session.get(DiaryEntry, entry_id)
    revision = session.get(DiaryRevision, revision_id)
    assert entry is not None and revision is not None
    memory = Memory(
        content=content,
        memory_type=memory_type,
        source_type="diary_entry",
        source_entry_id=entry.id,
        source_revision_id=revision.id,
        confidence=0.99,
        verification_status=status,
        is_active=status not in {"revoked", "superseded"},
        created_by="model",
    )
    session.add(memory)
    session.flush()
    session.add(
        MemorySource(
            memory_id=memory.id,
            entry_id=entry.id,
            revision_id=revision.id,
            evidence_text=evidence if evidence is not None else revision.content,
            start_offset=0 if evidence is None else None,
            end_offset=len(revision.content) if evidence is None else None,
        )
    )
    session.commit()
    session.refresh(memory)
    return memory


def test_memory_requires_primary_source_fields(client: TestClient, db_session) -> None:
    created = client.post("/api/v1/diaries", json={"content": "无来源测试"}).json()
    memory = Memory(
        content="这是一个没有来源的断言",
        memory_type="explicit_fact",
        source_type="diary_entry",
        source_entry_id=None,
        source_revision_id=None,
        verification_status="pending",
        is_active=True,
        created_by="model",
    )
    db_session.add(memory)
    try:
        db_session.commit()
        raised = False
    except IntegrityError:
        raised = True
        db_session.rollback()
    assert raised, "database must reject a memory without a primary source"
    assert created["id"]


def test_high_confidence_inference_cannot_be_auto_confirmed_by_raw_sql(
    client: TestClient, app, db_session
) -> None:
    created = client.post(
        "/api/v1/diaries",
        json={"content": "她今天没有回复我的消息，我有点失落。"},
    ).json()
    memory = _add_memory(
        db_session,
        entry_id=created["id"],
        revision_id=created["current_revision_id"],
        content="用户可能比较在意亲密关系中的回应",
        memory_type="model_inference",
        evidence="我有点失落",
    )
    assert memory.verification_status == "pending"
    assert memory.memory_type == "model_inference"

    with app.state.engine.begin() as connection:
        try:
            connection.execute(
                text("UPDATE memories SET verification_status = 'confirmed' WHERE id = :id"),
                {"id": memory.id},
            )
            raised = False
        except DatabaseError:
            raised = True
    assert raised, "a confirmation with no reviewing user must be rejected at the DB layer"

    check = db_session.get(Memory, memory.id)
    assert check is not None
    assert check.verification_status == "pending"
    assert check.memory_type == "model_inference"


def test_memory_source_points_at_original_revision(client: TestClient, db_session) -> None:
    created = client.post("/api/v1/diaries", json={"content": "原始日记第一版"}).json()
    updated = client.put(f"/api/v1/diaries/{created['id']}", json={"content": "原始日记第二版"}).json()
    memory = _add_memory(
        db_session,
        entry_id=created["id"],
        revision_id=created["current_revision_id"],
        content="第一条事实",
        memory_type="explicit_fact",
        evidence="原始日记第一版",
    )
    sources = db_session.query(MemorySource).filter_by(memory_id=memory.id).all()
    assert len(sources) == 1
    assert sources[0].revision_id == created["current_revision_id"]
    assert sources[0].revision_id != updated["current_revision_id"]
    source_revision = db_session.get(DiaryRevision, sources[0].revision_id)
    assert source_revision is not None
    assert source_revision.content == "原始日记第一版"
    assert sources[0].evidence_text in source_revision.content


def test_revoked_memory_excluded_from_search(client: TestClient, db_session) -> None:
    created = client.post("/api/v1/diaries", json={"content": "我们聊了很久"}).json()
    memory = _add_memory(
        db_session,
        entry_id=created["id"],
        revision_id=created["current_revision_id"],
        content="用户和对方聊了很久",
        memory_type="explicit_fact",
    )
    before = client.get("/api/v1/search", params={"q": "聊了很久", "scope": "memories"}).json()
    assert before["total"] == 1
    assert before["items"][0]["memory_id"] == memory.id

    memory.verification_status = "revoked"
    memory.is_active = False
    db_session.commit()
    after = client.get("/api/v1/search", params={"q": "聊了很久", "scope": "memories"}).json()
    assert after["total"] == 0


def test_contradictory_sources_are_kept_separate(client: TestClient, db_session) -> None:
    first = client.post("/api/v1/diaries", json={"content": "她说周末会来。"}).json()
    second = client.post("/api/v1/diaries", json={"content": "她说周末不会来。"}).json()
    m1 = _add_memory(
        db_session,
        entry_id=first["id"],
        revision_id=first["current_revision_id"],
        content="她说周末会来",
        memory_type="explicit_fact",
    )
    m2 = _add_memory(
        db_session,
        entry_id=second["id"],
        revision_id=second["current_revision_id"],
        content="她说周末不会来",
        memory_type="explicit_fact",
    )
    result = client.get("/api/v1/search", params={"q": "周末", "scope": "memories"}).json()
    ids = {item["memory_id"] for item in result["items"]}
    assert {m1.id, m2.id} <= ids
    assert m1.id != m2.id
    assert result["total"] == 2, "conflicting records must not be silently merged"


def test_permanent_delete_is_blocked_when_memories_reference_entry(client: TestClient, db_session) -> None:
    created = client.post("/api/v1/diaries", json={"content": "有记忆引用的日记"}).json()
    _add_memory(
        db_session,
        entry_id=created["id"],
        revision_id=created["current_revision_id"],
        content="一条已提取记忆",
        memory_type="explicit_fact",
    )
    headers = {"X-Admin-Token": "test-admin-token", "X-Confirm-Permanent-Delete": "true"}
    response = client.delete(f"/api/v1/diaries/{created['id']}/permanent", headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "entry_has_references"

    # Soft deletion must still work and keep both the revisions and the memory.
    assert client.delete(f"/api/v1/diaries/{created['id']}").status_code == 200
    with client.app.state.session_factory() as session:  # type: ignore[union-attr]
        assert session.get(DiaryEntry, created["id"]) is not None
        assert session.query(DiaryRevision).filter_by(entry_id=created["id"]).count() == 1
        assert session.query(Memory).filter_by(source_entry_id=created["id"]).count() == 1
