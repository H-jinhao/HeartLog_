"""Offline tests for weekly/monthly insights.

Disabled AI must return labelled local statistics; enabled AI must return a
narrative grounded in real stored facts, with real source references.  No test
performs a network request.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.core.config import Settings
from app.core.exceptions import AIServiceError
from app.core.hashing import sha256_text
from app.models.diary import DiaryEntry, DiaryRevision
from app.models.memory import Memory, MemorySource
from app.services.ai_service import AIService


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        purpose: str,
        json_mode: bool = False,
    ) -> str:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "purpose": purpose,
                "json_mode": json_mode,
            }
        )
        if not self.replies:
            raise AssertionError("fake provider received more calls than scripted replies")
        return self.replies.pop(0)


class FailingProvider:
    name = "failing"
    model = "failing-model"

    async def complete(self, **kwargs: Any) -> str:
        raise AIServiceError("upstream provider unavailable")


def _enable_ai(app: Any, provider: Any) -> AIService:
    ai_settings: Settings = app.state.settings.model_copy(
        update={
            "ai_enabled": True,
            "ai_consent": True,
            "ai_provider": "mock",
            "ai_model": provider.model,
        }
    )
    service = AIService(ai_settings, provider=provider)
    app.state.ai_service = service
    return service


def _create_record(
    db_session: Any,
    *,
    when: datetime,
    memory_type: str,
    memory_content: str = "她今天没有回复我的消息",
    evidence_text: str = "她今天没有回复我的消息",
    diary_text: str = "她今天没有回复我的消息，我有点失落。",
    verification_status: str = "pending",
    is_active: bool = True,
) -> dict[str, str]:
    """Insert one diary entry + revision + memory + source at a fixed time."""

    entry = DiaryEntry(
        original_text=diary_text,
        current_version=1,
        created_at=when,
        updated_at=when,
    )
    db_session.add(entry)
    db_session.flush()

    revision = DiaryRevision(
        entry_id=entry.id,
        version=1,
        content=diary_text,
        content_hash=sha256_text(diary_text),
        created_at=when,
    )
    db_session.add(revision)
    db_session.flush()

    entry.current_revision_id = revision.id

    memory = Memory(
        content=memory_content,
        content_hash="",
        memory_type=memory_type,
        source_type="diary_entry",
        source_entry_id=entry.id,
        source_revision_id=revision.id,
        confidence=0.8,
        verification_status=verification_status,
        is_active=is_active,
        created_by="model",
        created_at=when,
        updated_at=when,
    )
    db_session.add(memory)
    db_session.flush()

    db_session.add(
        MemorySource(
            memory_id=memory.id,
            entry_id=entry.id,
            revision_id=revision.id,
            evidence_text=evidence_text,
            created_at=when,
        )
    )
    db_session.commit()
    return {
        "entry_id": entry.id,
        "revision_id": revision.id,
        "memory_id": memory.id,
    }


def _user_payload(user_prompt: str) -> dict[str, Any]:
    begin = user_prompt.index("<<<BEGIN_UNTRUSTED_DATA>>>") + len("<<<BEGIN_UNTRUSTED_DATA>>>")
    end = user_prompt.index("<<<END_UNTRUSTED_DATA>>>")
    return json.loads(user_prompt[begin:end].strip())


# ---------------------------------------------------------------------------
# Disabled AI -> local statistics only, clearly labelled
# ---------------------------------------------------------------------------

WEEK_REFERENCE = date(2026, 3, 11)  # Wednesday; week starts Monday 2026-03-09
IN_WEEK = datetime(2026, 3, 10, 10, 0, tzinfo=UTC)
IN_WEEK_LATER = datetime(2026, 3, 11, 9, 0, tzinfo=UTC)
PREVIOUS_WEEK = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)


def test_weekly_disabled_returns_local_statistics_only(client, db_session) -> None:
    fact = _create_record(db_session, when=IN_WEEK, memory_type="explicit_fact")
    feeling = _create_record(db_session, when=IN_WEEK_LATER, memory_type="subjective_feeling")
    previous = _create_record(db_session, when=PREVIOUS_WEEK, memory_type="explicit_belief")

    response = client.get("/api/v1/insights/weekly", params={"date": WEEK_REFERENCE.isoformat()})
    assert response.status_code == 200, response.text
    data = response.json()

    assert data["mode"] == "local_statistics"
    assert data["ai_used"] is False
    assert data["narrative"] is None
    assert data["provider"] is None
    assert data["model"] is None
    assert data["disclaimer"]
    assert any("AI 未启用" in note for note in data["notes"])
    assert any("没有向任何外部提供方发送数据" in note for note in data["notes"])

    period = data["period"]
    assert period["period"] == "weekly"
    assert period["reference_date"] == WEEK_REFERENCE.isoformat()
    start = datetime.fromisoformat(period["start"])
    end = datetime.fromisoformat(period["end"])
    assert start == datetime(2026, 3, 9, 0, 0, tzinfo=UTC)
    assert end == start + timedelta(days=7)
    assert period["timezone"] == "UTC"

    statistics = data["statistics"]
    assert statistics["diary_entries"] == 2
    assert statistics["memories"] == 2
    counts = {item["memory_type"]: item["count"] for item in statistics["memory_type_counts"]}
    assert counts == {
        "explicit_fact": 1,
        "subjective_feeling": 1,
        "explicit_belief": 0,
        "model_inference": 0,
    }

    source_entry_ids = {item["entry_id"] for item in data["sources"]}
    assert {fact["entry_id"], feeling["entry_id"]} <= source_entry_ids
    assert previous["entry_id"] not in source_entry_ids
    memory_sources = {item["memory_id"]: item for item in data["sources"] if item["memory_id"]}
    assert fact["memory_id"] in memory_sources
    assert feeling["memory_id"] in memory_sources
    assert memory_sources[fact["memory_id"]]["snippet"] == "她今天没有回复我的消息"
    assert memory_sources[fact["memory_id"]]["version"] == 1


def test_monthly_disabled_uses_month_boundaries(client, db_session) -> None:
    march = _create_record(
        db_session, when=datetime(2026, 3, 10, 8, 0, tzinfo=UTC), memory_type="explicit_fact"
    )
    february = _create_record(
        db_session, when=datetime(2026, 2, 20, 8, 0, tzinfo=UTC), memory_type="explicit_fact"
    )
    april = _create_record(
        db_session, when=datetime(2026, 4, 2, 8, 0, tzinfo=UTC), memory_type="explicit_fact"
    )

    response = client.get("/api/v1/insights/monthly", params={"date": "2026-03-15"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["mode"] == "local_statistics"
    assert data["statistics"]["diary_entries"] == 1
    assert data["statistics"]["memories"] == 1
    assert datetime.fromisoformat(data["period"]["start"]) == datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    assert datetime.fromisoformat(data["period"]["end"]) == datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
    source_entry_ids = {item["entry_id"] for item in data["sources"]}
    assert march["entry_id"] in source_entry_ids
    assert february["entry_id"] not in source_entry_ids
    assert april["entry_id"] not in source_entry_ids


def test_default_period_is_current_week(client) -> None:
    response = client.get("/api/v1/insights/weekly")
    assert response.status_code == 200, response.text
    period = response.json()["period"]
    start = datetime.fromisoformat(period["start"])
    end = datetime.fromisoformat(period["end"])
    assert start.weekday() == 0
    assert end - start == timedelta(days=7)
    reference = date.fromisoformat(period["reference_date"])
    assert start.date() <= reference < end.date()


# ---------------------------------------------------------------------------
# AI enabled -> narrative grounded in local facts
# ---------------------------------------------------------------------------


def test_weekly_ai_narrative_uses_real_facts_and_sources(client, app, db_session) -> None:
    record = _create_record(
        db_session,
        when=IN_WEEK,
        memory_type="subjective_feeling",
        diary_text="PRIVATE_RAW_MARKER_9f2 她今天没有回复我的消息，我有点失落。",
    )
    narrative = "这一周你记录了「她今天没有回复我的消息」，并写下自己有点失落。"
    fake = FakeProvider([narrative])
    _enable_ai(app, fake)

    response = client.get("/api/v1/insights/weekly", params={"date": WEEK_REFERENCE.isoformat()})
    assert response.status_code == 200, response.text
    data = response.json()

    assert data["mode"] == "ai"
    assert data["ai_used"] is True
    assert data["narrative"] == narrative
    assert data["provider"] == "fake"
    assert data["model"] == "fake-model"
    assert data["statistics"]["diary_entries"] == 1
    assert data["statistics"]["memories"] == 1
    assert any(item["memory_id"] == record["memory_id"] for item in data["sources"])
    assert data["disclaimer"]
    assert any("AI" in note for note in data["notes"])

    assert len(fake.calls) == 1
    call = fake.calls[0]
    # Transport-level purpose is the generic "insight"; the logical review kind
    # is carried in the prompt payload.
    assert call["purpose"] == "insight"
    assert call["json_mode"] is False
    payload = _user_payload(call["user_prompt"])
    assert payload["purpose"] == "weekly_review"
    facts = payload["facts"]
    assert facts["statistics"]["diary_entries"] == 1
    assert facts["period"]["kind"] == "weekly"
    assert facts["memory_records"][0]["memory_id"] == record["memory_id"]
    # Privacy: raw diary text is not sent for review narratives, only memory snippets.
    assert "PRIVATE_RAW_MARKER_9f2" not in call["user_prompt"]
    assert "她今天没有回复我的消息" in call["user_prompt"]


def test_monthly_ai_narrative_uses_monthly_purpose(client, app, db_session) -> None:
    _create_record(
        db_session,
        when=datetime(2026, 3, 10, 8, 0, tzinfo=UTC),
        memory_type="explicit_fact",
    )
    fake = FakeProvider(["这个月你记录了一件事：她今天没有回复你的消息。"])
    _enable_ai(app, fake)
    response = client.get("/api/v1/insights/monthly", params={"date": "2026-03-15"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["mode"] == "ai"
    assert fake.calls[0]["purpose"] == "insight"
    monthly_payload = _user_payload(fake.calls[0]["user_prompt"])
    assert monthly_payload["purpose"] == "monthly_review"
    assert data["period"]["period"] == "monthly"


def test_ai_failure_falls_back_to_local_statistics_honestly(client, app, db_session) -> None:
    record = _create_record(db_session, when=IN_WEEK, memory_type="explicit_fact")
    _enable_ai(app, FailingProvider())

    response = client.get("/api/v1/insights/weekly", params={"date": WEEK_REFERENCE.isoformat()})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["mode"] == "local_statistics"
    assert data["ai_used"] is False
    assert data["narrative"] is None
    assert any("AI 叙事生成失败" in note for note in data["notes"])
    assert any(item["memory_id"] == record["memory_id"] for item in data["sources"])
    assert data["statistics"]["diary_entries"] == 1
