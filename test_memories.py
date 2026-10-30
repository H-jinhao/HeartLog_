"""Behavioural tests for the traceable memory subsystem.

Every test uses the isolated temporary SQLite database created by the shared
``conftest.py`` fixtures.  Diary rows are inserted directly through the
``db_session`` fixture so the memory tests do not depend on the (parallel)
diary-route workstream; ``DiaryEntry``/``DiaryRevision`` are the same ORM rows
the real diary API will write.

Covered invariants:

* extraction happy path, AI contract invocation against the exact revision;
* no source -> no memory (API, database and correction paths);
* fabricated evidence / bad offsets rejected with structured reasons;
* confidence never auto-confirms; ``model_inference`` type survives confirmation;
* confirm / correct / revoke state transitions and audit events;
* correction supersedes without rewriting; duplicate and conflict handling;
* transaction atomicity and per-candidate duplicate isolation;
* AI disabled -> 503; provider failure -> 502, nothing written;
* pagination/filters and default hiding of revoked/superseded rows.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.main import create_app
from app.models.audit import AuditEvent
from app.models.diary import DiaryEntry, DiaryRevision
from app.models.enums import MemoryType
from app.models.memory import Memory, MemorySource
from app.repositories.memory_repository import AuditRepository, MemoryRepository
from app.schemas.ai import CandidateMemory, ExtractionEnvelope

DIARY_TEXT = "今天她终于回复了我的消息，我很开心。不过下午我又有点担心她只是在敷衍我。"

EVIDENCE_HAPPY = "今天她终于回复了我的消息，我很开心"
EVIDENCE_WORRIED = "我又有点担心她只是在敷衍我"
EVIDENCE_WRONG = "她其实非常爱我"

API = "/api/v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class FakeAIService:
    """Deterministic stand-in for the injected ``AIService`` contract."""

    def __init__(
        self,
        candidates: list[CandidateMemory] | None = None,
        *,
        provider: str = "fake-provider",
        model: str = "fake-model",
        error: BaseException | None = None,
    ) -> None:
        self.candidates = candidates or []
        self.provider_name = provider
        self.model_name = model
        self.configured = True
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def extract_memories(
        self,
        *,
        entry_id: str,
        revision_id: str,
        version: int,
        content: str,
    ) -> ExtractionEnvelope:
        self.calls.append(
            {
                "entry_id": entry_id,
                "revision_id": revision_id,
                "version": version,
                "content": content,
            }
        )
        if self.error is not None:
            raise self.error
        return ExtractionEnvelope(
            candidates=self.candidates,
            provider=self.provider_name,
            model=self.model_name,
        )


def candidate(
    content: str,
    *,
    evidence: str | None = None,
    memory_type: MemoryType = MemoryType.EXPLICIT_FACT,
    confidence: float | None = 0.8,
    start_offset: int | None = None,
    end_offset: int | None = None,
) -> CandidateMemory:
    return CandidateMemory(
        content=content,
        memory_type=memory_type,
        confidence=confidence,
        evidence_text=evidence if evidence is not None else content,
        start_offset=start_offset,
        end_offset=end_offset,
    )


def make_diary_entry(
    db: Session,
    content: str = DIARY_TEXT,
    *,
    extra_versions: tuple[str, ...] = (),
) -> tuple[DiaryEntry, list[DiaryRevision]]:
    """Insert a diary entry + revisions directly through the ORM."""

    entry = DiaryEntry(original_text=content)
    db.add(entry)
    db.flush()
    revision = DiaryRevision(entry_id=entry.id, version=1, content=content)
    db.add(revision)
    db.flush()
    revisions = [revision]
    entry.current_version = 1
    entry.current_revision_id = revision.id
    for next_content in extra_versions:
        next_revision = DiaryRevision(
            entry_id=entry.id,
            version=len(revisions) + 1,
            content=next_content,
            previous_revision_hash=revisions[-1].content_hash,
        )
        db.add(next_revision)
        db.flush()
        revisions.append(next_revision)
        entry.current_version = next_revision.version
        entry.current_revision_id = next_revision.id
    db.commit()
    return entry, revisions


def install_ai(app: Any, fake: FakeAIService) -> None:
    app.state.ai_service = fake


def run_extraction(
    client: TestClient,
    app: Any,
    entry: DiaryEntry,
    candidates: list[CandidateMemory],
    **request_fields: Any,
) -> tuple[Any, FakeAIService]:
    fake = FakeAIService(candidates)
    install_ai(app, fake)
    payload: dict[str, Any] = {"entry_id": entry.id}
    payload.update(request_fields)
    return client.post(f"{API}/memories/extract", json=payload), fake


def refresh(db: Session) -> None:
    """End any stale read transaction so later queries see committed writes."""

    db.rollback()


def count_rows(db: Session, model: type[Any]) -> int:
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


def listed_ids(response_json: dict[str, Any]) -> list[str]:
    return [item["id"] for item in response_json["items"]]


def assert_error(response: Any, status_code: int, code: str) -> dict[str, Any]:
    assert response.status_code == status_code, response.text
    body = response.json()
    assert body["error"]["code"] == code, body
    return body["error"]


def audit_events(db: Session) -> list[AuditEvent]:
    refresh(db)
    return list(db.scalars(select(AuditEvent).order_by(AuditEvent.created_at, AuditEvent.id)).all())


# ---------------------------------------------------------------------------
# Extraction: happy path + traceability
# ---------------------------------------------------------------------------
def test_extract_persists_pending_model_memories(client, app, db_session):
    entry, revisions = make_diary_entry(db_session)
    revision = revisions[0]
    candidates = [
        candidate("她回复了我的消息，我很开心", evidence=EVIDENCE_HAPPY, confidence=0.82),
        candidate(
            "她可能在敷衍我，我有些不安",
            evidence=EVIDENCE_WORRIED,
            memory_type=MemoryType.SUBJECTIVE_FEELING,
            confidence=0.61,
        ),
    ]
    response, fake = run_extraction(client, app, entry, candidates)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created_count"] == 2
    assert body["rejected_count"] == 0
    assert body["entry_id"] == entry.id
    assert body["revision_id"] == revision.id
    assert body["version"] == 1
    assert body["provider"] == "fake-provider"
    assert len(fake.calls) == 1

    for item in body["created"]:
        assert item["verification_status"] == "pending"
        assert item["created_by"] == "model"
        assert item["is_active"] is True
        assert item["confirmed_by"] is None
        assert item["source_entry_id"] == entry.id
        assert item["source_revision_id"] == revision.id
        assert item["source_version"] == 1
        assert len(item["sources"]) >= 1
        assert item["sources"][0]["revision_id"] == revision.id

    # Atomic persistence: memories + one source each + audit trail.
    refresh(db_session)
    assert count_rows(db_session, Memory) == 2
    assert count_rows(db_session, MemorySource) == 2
    created_audits = [event for event in audit_events(db_session) if event.event_type == "memory.created"]
    assert len(created_audits) == 2
    assert len(created_audits[0].details["memory_id"]) == 36


def test_extract_traces_evidence_to_exact_revision(client, app, db_session):
    entry, revisions = make_diary_entry(db_session)
    revision = revisions[0]
    start = DIARY_TEXT.index(EVIDENCE_HAPPY)
    end = start + len(EVIDENCE_HAPPY)
    candidates = [
        candidate(
            "她回复了我的消息，我很开心",
            evidence=EVIDENCE_HAPPY,
            start_offset=start,
            end_offset=end,
        )
    ]
    response, fake = run_extraction(client, app, entry, candidates)

    assert response.status_code == 200, response.text
    # The AI adapter received the exact immutable revision, not a cached text.
    assert fake.calls[0]["entry_id"] == entry.id
    assert fake.calls[0]["revision_id"] == revision.id
    assert fake.calls[0]["version"] == revision.version
    assert fake.calls[0]["content"] == revision.content

    memory = response.json()["created"][0]
    assert memory["source_revision_id"] == revision.id
    assert memory["source_version"] == revision.version
    source = memory["sources"][0]
    assert source["entry_id"] == entry.id
    assert source["revision_id"] == revision.id
    assert source["evidence_text"] == revision.content[start:end]
    assert source["start_offset"] == start
    assert source["end_offset"] == end

    # Detail endpoint and dedicated sources endpoint expose the same trace.
    detail = client.get(f"{API}/memories/{memory['id']}")
    assert detail.status_code == 200
    assert detail.json()["sources"][0]["evidence_text"] == EVIDENCE_HAPPY

    sources = client.get(f"{API}/memories/{memory['id']}/sources")
    assert sources.status_code == 200
    sources_body = sources.json()
    assert sources_body["total"] == 1
    assert sources_body["sources"][0]["revision_version"] == revision.version
    assert sources_body["sources"][0]["revision_id"] == revision.id


def test_extract_targets_older_revision_by_version(client, app, db_session):
    entry, revisions = make_diary_entry(
        db_session,
        DIARY_TEXT,
        extra_versions=("第二版：她后来解释说她只是忙。",),
    )
    response, fake = run_extraction(
        client, app, entry, [candidate("她解释说她只是忙", evidence=EVIDENCE_HAPPY)], version=1
    )

    assert response.status_code == 200, response.text
    assert fake.calls[0]["content"] == revisions[0].content
    assert fake.calls[0]["revision_id"] == revisions[0].id
    assert response.json()["created"][0]["source_revision_id"] == revisions[0].id
    assert response.json()["created"][0]["source_version"] == 1


# ---------------------------------------------------------------------------
# Extraction: evidence validation and rejection reasons
# ---------------------------------------------------------------------------
def test_fabricated_evidence_is_rejected_and_nothing_is_written(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    response, _ = run_extraction(client, app, entry, [candidate("她其实非常爱我", evidence=EVIDENCE_WRONG)])

    error = assert_error(response, 422, "no_valid_candidates")
    assert error["details"]["candidate_count"] == 1
    assert error["details"]["rejected"][0]["reason"] == "evidence_not_found"
    # Rejected candidates never echo the fabricated / private text back.
    assert EVIDENCE_WRONG not in response.text
    assert DIARY_TEXT not in response.text

    refresh(db_session)
    assert count_rows(db_session, Memory) == 0
    assert count_rows(db_session, MemorySource) == 0
    assert count_rows(db_session, AuditEvent) == 0


def test_evidence_offsets_must_match_exact_revision_slice(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    start = DIARY_TEXT.index(EVIDENCE_HAPPY)
    response, _ = run_extraction(
        client,
        app,
        entry,
        [
            candidate(
                "她回复消息我很开心",
                evidence=EVIDENCE_HAPPY,
                start_offset=start + 1,
                end_offset=start + 1 + len(EVIDENCE_HAPPY),
            )
        ],
    )
    error = assert_error(response, 422, "no_valid_candidates")
    assert error["details"]["rejected"][0]["reason"] == "evidence_offset_mismatch"

    refresh(db_session)
    assert count_rows(db_session, Memory) == 0


def test_evidence_offsets_beyond_content_are_rejected(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    response, _ = run_extraction(
        client,
        app,
        entry,
        [
            candidate(
                "过度偏移",
                evidence=EVIDENCE_HAPPY,
                start_offset=10_000,
                end_offset=10_000 + len(EVIDENCE_HAPPY),
            )
        ],
    )
    error = assert_error(response, 422, "no_valid_candidates")
    assert error["details"]["rejected"][0]["reason"] == "evidence_offset_mismatch"


def test_blank_candidate_content_is_rejected(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    response, _ = run_extraction(client, app, entry, [candidate("   ", evidence=EVIDENCE_HAPPY)])
    error = assert_error(response, 422, "no_valid_candidates")
    assert error["details"]["rejected"][0]["reason"] == "content_blank"


def test_mixed_batch_creates_only_valid_candidates(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    candidates = [
        candidate("她回复了我的消息，我很开心", evidence=EVIDENCE_HAPPY),
        candidate("她其实非常爱我", evidence=EVIDENCE_WRONG),
        candidate("我有点不安", evidence=EVIDENCE_WORRIED, memory_type=MemoryType.SUBJECTIVE_FEELING),
    ]
    response, _ = run_extraction(client, app, entry, candidates)
    body = response.json()
    assert response.status_code == 200, response.text
    assert body["candidate_count"] == 3
    assert body["created_count"] == 2
    assert body["rejected_count"] == 1
    assert body["rejected"][0]["index"] == 1
    assert body["rejected"][0]["reason"] == "evidence_not_found"

    refresh(db_session)
    assert count_rows(db_session, Memory) == 2
    assert count_rows(db_session, MemorySource) == 2


def test_rejected_candidate_reports_original_index(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    candidates = [
        candidate("第一条记忆", evidence=EVIDENCE_HAPPY),
        candidate("第二条记忆", evidence=EVIDENCE_WRONG),
        candidate("第三条记忆", evidence=EVIDENCE_WORRIED, memory_type=MemoryType.SUBJECTIVE_FEELING),
    ]
    response, _ = run_extraction(client, app, entry, candidates)
    assert response.status_code == 200, response.text
    assert response.json()["rejected"][0]["index"] == 1


# ---------------------------------------------------------------------------
# Extraction: revision targeting errors
# ---------------------------------------------------------------------------
def test_extract_unknown_entry_returns_404(client, app, db_session):
    make_diary_entry(db_session)  # ensure schema/data exists
    fake = FakeAIService([candidate("x", evidence=EVIDENCE_HAPPY)])
    install_ai(app, fake)
    response = client.post(f"{API}/memories/extract", json={"entry_id": "missing-entry"})
    assert_error(response, 404, "not_found")
    assert fake.calls == []


def test_revision_must_belong_to_entry(client, app, db_session):
    entry_a, _ = make_diary_entry(db_session)
    entry_b, revisions_b = make_diary_entry(db_session, "另一个人的日记：今天下雨了。")
    fake = FakeAIService([candidate("x", evidence=EVIDENCE_HAPPY)])
    install_ai(app, fake)
    response = client.post(
        f"{API}/memories/extract",
        json={"entry_id": entry_a.id, "revision_id": revisions_b[0].id},
    )
    assert_error(response, 404, "revision_not_found")
    assert fake.calls == []
    refresh(db_session)
    assert count_rows(db_session, Memory) == 0


def test_revision_and_version_mismatch_returns_422(client, app, db_session):
    entry, revisions = make_diary_entry(
        db_session,
        DIARY_TEXT,
        extra_versions=("第二版内容。",),
    )
    fake = FakeAIService([candidate("x", evidence=EVIDENCE_HAPPY)])
    install_ai(app, fake)
    response = client.post(
        f"{API}/memories/extract",
        json={"entry_id": entry.id, "revision_id": revisions[0].id, "version": 2},
    )
    assert_error(response, 422, "revision_mismatch")
    assert fake.calls == []
    refresh(db_session)
    assert count_rows(db_session, Memory) == 0


def test_requested_missing_version_returns_404(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    fake = FakeAIService([candidate("x", evidence=EVIDENCE_HAPPY)])
    install_ai(app, fake)
    response = client.post(f"{API}/memories/extract", json={"entry_id": entry.id, "version": 99})
    assert_error(response, 404, "revision_not_found")
    assert fake.calls == []


def test_extract_from_deleted_entry_returns_409_no_write(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    stored = db_session.get(DiaryEntry, entry.id)
    assert stored is not None
    stored.deleted_at = datetime.now(UTC)
    db_session.commit()

    fake = FakeAIService([candidate("x", evidence=EVIDENCE_HAPPY)])
    install_ai(app, fake)
    response = client.post(f"{API}/memories/extract", json={"entry_id": entry.id})
    assert_error(response, 409, "diary_deleted")
    assert fake.calls == []
    refresh(db_session)
    assert count_rows(db_session, Memory) == 0


# ---------------------------------------------------------------------------
# Extraction: AI failure paths
# ---------------------------------------------------------------------------
def test_ai_disabled_returns_503_and_writes_nothing(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    # Default app state: AI is disabled/unconfigured.
    response = client.post(f"{API}/memories/extract", json={"entry_id": entry.id})
    assert_error(response, 503, "ai_not_configured")
    refresh(db_session)
    assert count_rows(db_session, Memory) == 0
    assert count_rows(db_session, AuditEvent) == 0


def test_ai_provider_failure_returns_502_and_writes_nothing(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    fake = FakeAIService([candidate("x", evidence=EVIDENCE_HAPPY)], error=RuntimeError("provider exploded"))
    install_ai(app, fake)
    response = client.post(f"{API}/memories/extract", json={"entry_id": entry.id})
    assert_error(response, 502, "ai_service_error")
    refresh(db_session)
    assert count_rows(db_session, Memory) == 0
    assert count_rows(db_session, AuditEvent) == 0


def test_empty_candidate_list_is_a_clean_noop(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    response, _ = run_extraction(client, app, entry, [])
    assert response.status_code == 200, response.text
    assert response.json()["created_count"] == 0
    refresh(db_session)
    assert count_rows(db_session, Memory) == 0
    types = [event.event_type for event in audit_events(db_session)]
    assert types == ["memories.extracted"]


# ---------------------------------------------------------------------------
# Review semantics: confidence, confirmation, immutability of type
# ---------------------------------------------------------------------------
def test_high_confidence_inference_stays_pending_and_keeps_its_type(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    response, _ = run_extraction(
        client,
        app,
        entry,
        [
            candidate(
                "她可能对我有好感",
                evidence=EVIDENCE_HAPPY,
                memory_type=MemoryType.MODEL_INFERENCE,
                confidence=0.99,
            )
        ],
    )
    memory = response.json()["created"][0]
    assert memory["confidence"] == 0.99
    assert memory["verification_status"] == "pending"
    assert memory["created_by"] == "model"
    assert memory["memory_type"] == "model_inference"

    confirmed = client.post(f"{API}/memories/{memory['id']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    confirmed_body = confirmed.json()
    assert confirmed_body["verification_status"] == "confirmed"
    assert confirmed_body["confirmed_by"] == "user"
    assert confirmed_body["confirmed_at"] is not None
    # The type is evidence about the claim's origin, so it never changes.
    assert confirmed_body["memory_type"] == "model_inference"

    refresh(db_session)
    stored = db_session.get(Memory, memory["id"])
    assert stored is not None
    assert stored.verification_status == "confirmed"
    assert stored.memory_type == "model_inference"
    assert stored.created_by == "model"


def test_confirm_pending_memory_sets_user_and_audit(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    response, _ = run_extraction(client, app, entry, [candidate("她回复了我的消息", evidence=EVIDENCE_HAPPY)])
    memory_id = response.json()["created"][0]["id"]

    confirm = client.post(f"{API}/memories/{memory_id}/confirm")
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["verification_status"] == "confirmed"

    events = [event for event in audit_events(db_session) if event.event_type == "memory.confirmed"]
    assert len(events) == 1
    assert events[0].actor == "user"
    assert events[0].details["previous_status"] == "pending"
    assert events[0].details["new_status"] == "confirmed"
    assert events[0].details["confirmed_by"] == "user"
    assert events[0].entity_id == memory_id

    # A second confirmation is a conflict, not a silent no-op.
    again = client.post(f"{API}/memories/{memory_id}/confirm")
    assert_error(again, 409, "already_confirmed")


def test_confirm_unknown_memory_returns_404(client):
    response = client.post(f"{API}/memories/does-not-exist/confirm")
    assert_error(response, 404, "not_found")


# ---------------------------------------------------------------------------
# Correction: append-only supersede
# ---------------------------------------------------------------------------
def test_correction_creates_new_memory_and_supersedes_old(client, app, db_session):
    entry, revisions = make_diary_entry(db_session)
    revision = revisions[0]
    response, _ = run_extraction(
        client, app, entry, [candidate("她回复了我的消息，我很开心", evidence=EVIDENCE_HAPPY)]
    )
    original = response.json()["created"][0]
    client.post(f"{API}/memories/{original['id']}/confirm")

    corrected_content = "她回复了我的消息，我更多的感受是放松"
    corrected = client.post(
        f"{API}/memories/{original['id']}/correct",
        json={"content": corrected_content, "change_reason": "更准确的感受描述"},
    )
    assert corrected.status_code == 201, corrected.text
    corrected_body = corrected.json()
    assert corrected_body["id"] != original["id"]
    assert corrected_body["content"] == corrected_content
    assert corrected_body["memory_type"] == original["memory_type"]
    assert corrected_body["source_type"] == "user_correction"
    assert corrected_body["created_by"] == "user"
    assert corrected_body["verification_status"] == "confirmed"
    assert corrected_body["confirmed_by"] == "user"
    assert corrected_body["supersedes_memory_id"] == original["id"]
    assert corrected_body["source_entry_id"] == revision.entry_id
    assert corrected_body["source_revision_id"] == revision.id
    assert corrected_body["source_version"] == 1
    # Evidence is copied and re-validated, never fabricated.
    assert len(corrected_body["sources"]) == len(original["sources"])
    assert corrected_body["sources"][0]["evidence_text"] == EVIDENCE_HAPPY
    assert corrected_body["metadata"]["correction"]["corrected_memory_id"] == original["id"]

    refresh(db_session)
    old = db_session.get(Memory, original["id"])
    assert old is not None
    # The old row is state-flipped, never rewritten.
    assert old.content == original["content"]
    assert old.content_hash == original["content_hash"]
    assert old.verification_status == "superseded"
    assert old.is_active is False
    new = db_session.get(Memory, corrected_body["id"])
    assert new is not None and new.is_active is True
    assert count_rows(db_session, MemorySource) == 2

    # Default list (and therefore normal search views) hides the superseded row.
    listed = client.get(f"{API}/memories").json()
    assert listed["total"] == 1
    assert listed_ids(listed) == [corrected_body["id"]]

    event_types = [event.event_type for event in audit_events(db_session)]
    assert "memory.corrected" in event_types


def test_correction_with_unchanged_content_is_rejected(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    response, _ = run_extraction(client, app, entry, [candidate("她回复了我的消息", evidence=EVIDENCE_HAPPY)])
    original = response.json()["created"][0]

    result = client.post(f"{API}/memories/{original['id']}/correct", json={"content": original["content"]})
    assert_error(result, 422, "correction_unchanged")
    refresh(db_session)
    assert count_rows(db_session, Memory) == 1


def test_correction_refuses_revoked_memory(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    response, _ = run_extraction(client, app, entry, [candidate("她回复了我的消息", evidence=EVIDENCE_HAPPY)])
    memory_id = response.json()["created"][0]["id"]
    client.post(f"{API}/memories/{memory_id}/revoke")

    result = client.post(f"{API}/memories/{memory_id}/correct", json={"content": "改进后的内容"})
    assert_error(result, 409, "invalid_state_transition")


def test_correction_duplicate_content_conflicts_cleanly(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    candidates = [
        candidate("她的回复让我很开心", evidence=EVIDENCE_HAPPY),
        # Same memory_type so the unique (revision, content_hash, type) guard
        # is what the correction collides with.
        candidate("她的沉默让我不安", evidence=EVIDENCE_WORRIED, memory_type=MemoryType.EXPLICIT_FACT),
    ]
    response, _ = run_extraction(client, app, entry, candidates)
    first, second = response.json()["created"]

    # Correcting the first memory to an existing claim from the same revision
    # must not violate the unique constraint at commit time.
    result = client.post(f"{API}/memories/{first['id']}/correct", json={"content": second["content"]})
    assert_error(result, 409, "duplicate_memory")
    refresh(db_session)
    assert count_rows(db_session, Memory) == 2


def test_correction_refuses_memory_without_sources(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    response, _ = run_extraction(client, app, entry, [candidate("她回复了我的消息", evidence=EVIDENCE_HAPPY)])
    memory_id = response.json()["created"][0]["id"]

    # Simulate corruption: remove evidence rows directly (no supported path can
    # create a source-less memory in the first place).
    db_session.execute(delete(MemorySource).where(MemorySource.memory_id == memory_id))
    db_session.commit()
    refresh(db_session)

    result = client.post(f"{API}/memories/{memory_id}/correct", json={"content": "新内容"})
    assert_error(result, 409, "missing_sources")
    refresh(db_session)
    assert count_rows(db_session, Memory) == 1


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------
def test_revoke_hides_memory_from_normal_list_but_keeps_it_traceable(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    candidates = [
        candidate("可以保留的记忆", evidence=EVIDENCE_HAPPY),
        candidate("要被撤销的记忆", evidence=EVIDENCE_WORRIED, memory_type=MemoryType.SUBJECTIVE_FEELING),
    ]
    response, _ = run_extraction(client, app, entry, candidates)
    keep, revoke = response.json()["created"]

    revoked = client.post(f"{API}/memories/{revoke['id']}/revoke", json={"reason": "用户认为不准确"})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["verification_status"] == "revoked"
    assert revoked.json()["is_active"] is False
    assert revoked.json()["revoked_at"] is not None

    default_list = client.get(f"{API}/memories").json()
    assert default_list["total"] == 1
    assert listed_ids(default_list) == [keep["id"]]

    inactive_list = client.get(f"{API}/memories", params={"active": "false"}).json()
    assert inactive_list["total"] == 1
    assert listed_ids(inactive_list) == [revoke["id"]]

    revoked_list = client.get(f"{API}/memories", params={"verification_status": "revoked"}).json()
    assert revoked_list["total"] == 1
    assert revoked_list["items"][0]["verification_status"] == "revoked"

    # Still retrievable explicitly: revocation is not silent deletion.
    detail = client.get(f"{API}/memories/{revoke['id']}")
    assert detail.status_code == 200
    assert detail.json()["sources"][0]["evidence_text"] == EVIDENCE_WORRIED

    refresh(db_session)
    stored = db_session.get(Memory, revoke["id"])
    assert stored is not None
    assert stored.verification_status == "revoked"
    assert stored.is_active is False
    assert stored.content == revoke["content"]  # content never rewritten

    event_types = [event.event_type for event in audit_events(db_session)]
    assert "memory.revoked" in event_types


def test_revoke_twice_conflicts(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    response, _ = run_extraction(client, app, entry, [candidate("要撤销的记忆", evidence=EVIDENCE_HAPPY)])
    memory_id = response.json()["created"][0]["id"]
    assert client.post(f"{API}/memories/{memory_id}/revoke").status_code == 200
    assert_error(client.post(f"{API}/memories/{memory_id}/revoke"), 409, "already_revoked")


def test_unknown_memory_endpoints_return_404(client):
    assert_error(client.get(f"{API}/memories/nope"), 404, "not_found")
    assert_error(client.get(f"{API}/memories/nope/sources"), 404, "not_found")
    assert_error(client.post(f"{API}/memories/nope/correct", json={"content": "x"}), 404, "not_found")
    assert_error(client.post(f"{API}/memories/nope/revoke"), 404, "not_found")


# ---------------------------------------------------------------------------
# Conflicts and duplicates
# ---------------------------------------------------------------------------
def test_contradictory_memories_are_preserved_separately(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    candidates = [
        candidate("她的回复让我很开心", evidence=EVIDENCE_HAPPY),
        candidate("她的回复让我更焦虑", evidence=EVIDENCE_WORRIED, memory_type=MemoryType.SUBJECTIVE_FEELING),
    ]
    response, _ = run_extraction(client, app, entry, candidates)
    created = response.json()["created"]
    assert len(created) == 2
    assert created[0]["id"] != created[1]["id"]
    assert {item["content"] for item in created} == {"她的回复让我很开心", "她的回复让我更焦虑"}
    assert all(item["verification_status"] == "pending" for item in created)
    refresh(db_session)
    assert count_rows(db_session, Memory) == 2


def test_duplicate_extraction_is_skipped_not_duplicated(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    candidates = [candidate("她回复了我的消息", evidence=EVIDENCE_HAPPY)]
    first, _ = run_extraction(client, app, entry, candidates)
    assert first.status_code == 200
    second, _ = run_extraction(client, app, entry, candidates)
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["created_count"] == 0
    assert body["rejected_count"] == 1
    assert body["rejected"][0]["reason"] == "duplicate_memory"
    refresh(db_session)
    assert count_rows(db_session, Memory) == 1
    assert count_rows(db_session, MemorySource) == 1


def test_duplicate_insert_race_is_isolated_without_poisoning_batch(client, app, db_session, monkeypatch):
    entry, _ = make_diary_entry(db_session)
    original_candidate = candidate("她回复了我的消息", evidence=EVIDENCE_HAPPY)
    first, _ = run_extraction(client, app, entry, [original_candidate])
    assert first.status_code == 200

    original_find = MemoryRepository.find_duplicate
    state = {"calls": 0}

    def stale_first_lookup(self, **kwargs):  # simulate a lost race on the pre-check
        state["calls"] += 1
        if state["calls"] == 1:
            return None
        return original_find(self, **kwargs)

    monkeypatch.setattr(MemoryRepository, "find_duplicate", stale_first_lookup)

    new_candidate = candidate("我有点担心她在敷衍我", evidence=EVIDENCE_WORRIED)
    response, _ = run_extraction(client, app, entry, [original_candidate, new_candidate])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created_count"] == 1
    assert body["rejected_count"] == 1
    assert body["rejected"][0]["reason"] == "duplicate_memory"
    assert body["created"][0]["content"] == new_candidate.content

    refresh(db_session)
    assert count_rows(db_session, Memory) == 2
    assert count_rows(db_session, MemorySource) == 2


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------
def test_database_error_rolls_back_entire_extraction(client, app, db_session, monkeypatch):
    entry, _ = make_diary_entry(db_session)
    original_add = AuditRepository.add
    state = {"calls": 0}

    def failing_add(self, **kwargs):
        state["calls"] += 1
        if state["calls"] == 2:  # memory.created, then memories.extracted -> fail
            raise SQLAlchemyError("simulated audit write failure")
        return original_add(self, **kwargs)

    monkeypatch.setattr(AuditRepository, "add", failing_add)

    response, _ = run_extraction(client, app, entry, [candidate("她回复了我的消息", evidence=EVIDENCE_HAPPY)])
    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "database_error"

    refresh(db_session)
    assert count_rows(db_session, Memory) == 0
    assert count_rows(db_session, MemorySource) == 0
    assert count_rows(db_session, AuditEvent) == 0


# ---------------------------------------------------------------------------
# Listing: pagination, filters, response shape
# ---------------------------------------------------------------------------
def test_list_pagination_and_filters(client, app, db_session):
    entry, revisions = make_diary_entry(db_session)
    revision = revisions[0]
    candidates = [
        candidate("事实一", evidence=EVIDENCE_HAPPY, memory_type=MemoryType.EXPLICIT_FACT),
        candidate("感受一", evidence=EVIDENCE_WORRIED, memory_type=MemoryType.SUBJECTIVE_FEELING),
        candidate("推断一", evidence=EVIDENCE_HAPPY, memory_type=MemoryType.MODEL_INFERENCE, confidence=0.7),
    ]
    response, _ = run_extraction(client, app, entry, candidates)
    assert response.status_code == 200, response.text

    default_page = client.get(f"{API}/memories").json()
    assert default_page["total"] == 3
    assert default_page["limit"] == 20
    assert default_page["offset"] == 0
    assert default_page["has_more"] is False

    first_page = client.get(f"{API}/memories", params={"limit": 2}).json()
    assert first_page["total"] == 3
    assert len(first_page["items"]) == 2
    assert first_page["has_more"] is True

    second_page = client.get(f"{API}/memories", params={"limit": 2, "offset": 2}).json()
    assert len(second_page["items"]) == 1
    assert second_page["has_more"] is False
    assert set(listed_ids(first_page) + listed_ids(second_page)) == set(
        item["id"] for item in default_page["items"]
    )

    by_type = client.get(f"{API}/memories", params={"memory_type": "model_inference"}).json()
    assert by_type["total"] == 1
    assert by_type["items"][0]["memory_type"] == "model_inference"
    assert by_type["items"][0]["source_revision_id"] == revision.id

    by_status = client.get(f"{API}/memories", params={"verification_status": "pending"}).json()
    assert by_status["total"] == 3

    by_entry = client.get(f"{API}/memories", params={"entry_id": entry.id}).json()
    assert by_entry["total"] == 3
    assert all(item["source_entry_id"] == entry.id for item in by_entry["items"])

    inactive = client.get(f"{API}/memories", params={"active": "false"}).json()
    assert inactive["total"] == 0

    clamped = client.get(f"{API}/memories", params={"limit": 500}).json()
    assert clamped["limit"] == 100  # settings.max_page_size


# ---------------------------------------------------------------------------
# Audit trail privacy / structure
# ---------------------------------------------------------------------------
def test_audit_details_are_structural_and_never_copy_diary_text(client, app, db_session):
    entry, _ = make_diary_entry(db_session)
    response, _ = run_extraction(client, app, entry, [candidate("她回复了我的消息", evidence=EVIDENCE_HAPPY)])
    memory_id = response.json()["created"][0]["id"]
    client.post(f"{API}/memories/{memory_id}/confirm")
    corrected = client.post(f"{API}/memories/{memory_id}/correct", json={"content": "她回复消息，我感到放松"})
    client.post(f"{API}/memories/{corrected.json()['id']}/revoke", json={"reason": "用户要求撤销"})

    events = audit_events(db_session)
    assert events, "expected audit events to be written"
    payload = json.dumps(
        [
            {
                "event_type": event.event_type,
                "entity_type": event.entity_type,
                "details": event.details,
            }
            for event in events
        ],
        ensure_ascii=False,
    )
    assert DIARY_TEXT not in payload
    assert EVIDENCE_HAPPY not in payload
    assert EVIDENCE_WORRIED not in payload

    forbidden_keys = {"content", "evidence_text", "original_text"}
    keys: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                keys.append(str(key))
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for event in events:
        walk(event.details)
    assert not (forbidden_keys & set(keys))


# ---------------------------------------------------------------------------
# Settings-driven width limits
# ---------------------------------------------------------------------------
def test_settings_max_evidence_chars_is_enforced(tmp_path):
    settings = Settings(
        environment="test",
        database_url=f"sqlite:///{(tmp_path / 'width.db').as_posix()}",
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "backups",
        auto_create_schema=True,
        max_evidence_chars=10,
        ai_enabled=False,
        ai_consent=False,
        ai_provider="disabled",
        _env_file=None,
    )
    application = create_app(settings)
    try:
        with application.state.session_factory() as session:
            entry = DiaryEntry(original_text=DIARY_TEXT)
            session.add(entry)
            session.flush()
            revision = DiaryRevision(entry_id=entry.id, version=1, content=DIARY_TEXT)
            session.add(revision)
            session.flush()
            entry.current_version = 1
            entry.current_revision_id = revision.id
            session.commit()
            entry_id = entry.id

        fake = FakeAIService([candidate("短记忆", evidence=DIARY_TEXT[:15])])
        application.state.ai_service = fake
        with TestClient(application) as local_client:
            response = local_client.post(f"{API}/memories/extract", json={"entry_id": entry_id})
        error = assert_error(response, 422, "no_valid_candidates")
        assert error["details"]["rejected"][0]["reason"] == "evidence_too_long"
    finally:
        application.state.engine.dispose()
