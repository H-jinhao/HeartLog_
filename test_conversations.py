"""Offline tests for conversations: disabled placeholder mode and fake-AI mode.

No test performs a network request.  The fake provider records the prompts it
receives so we can assert that context/citations are grounded in the database.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.core.config import Settings
from app.core.hashing import sha256_text
from app.models.diary import DiaryEntry, DiaryRevision
from app.models.memory import Memory, MemorySource
from app.services.ai_service import AIService


class FakeProvider:
    """Deterministic scripted provider (no network)."""

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


def _enable_ai(app: Any, provider: FakeProvider) -> AIService:
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


def _create_memory(
    db_session: Any,
    *,
    content: str = "她今天没有回复我的消息，我有点失落。",
    memory_content: str = "她今天没有回复我的消息",
    memory_type: str = "subjective_feeling",
    evidence_text: str = "她今天没有回复我的消息",
    verification_status: str = "pending",
    is_active: bool = True,
) -> dict[str, str]:
    """Insert a DiaryEntry + DiaryRevision + Memory + MemorySource.

    Diary endpoints are owned by another workstream and may still be in progress,
    so tests create the immutable rows directly through the ORM with helpers.
    """

    entry = DiaryEntry(original_text=content, current_version=1)
    db_session.add(entry)
    db_session.flush()

    revision = DiaryRevision(
        entry_id=entry.id,
        version=1,
        content=content,
        content_hash=sha256_text(content),
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
    )
    db_session.add(memory)
    db_session.flush()

    db_session.add(
        MemorySource(
            memory_id=memory.id,
            entry_id=entry.id,
            revision_id=revision.id,
            evidence_text=evidence_text,
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
# Disabled AI -> labelled placeholder, never pretend
# ---------------------------------------------------------------------------


def test_create_conversation_and_placeholder_message(client, monkeypatch) -> None:
    # Prove the disabled path never even constructs a network client.
    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no HTTP client may be created when AI is disabled")

    monkeypatch.setattr(httpx, "AsyncClient", _explode)

    response = client.post("/api/v1/conversations", json={"title": "测试会话"})
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["title"] == "测试会话"
    conversation_id = created["id"]

    response = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "今天我有点累。"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["mode"] == "placeholder"
    assert data["ai_used"] is False
    assert data["citations"] == []
    assert data["provider"] is None
    assert data["assistant_message"]["mode"] == "placeholder"
    assert "占位模式" in data["assistant_message"]["content"]
    assert data["notice"]
    assert data["user_message"]["content"] == "今天我有点累。"

    detail = client.get(f"/api/v1/conversations/{conversation_id}")
    assert detail.status_code == 200, detail.text
    stored = detail.json()
    assert [message["role"] for message in stored["messages"]] == ["user", "assistant"]
    assert stored["messages"][0]["content"] == "今天我有点累。"
    assert stored["messages"][1]["mode"] == "placeholder"
    assert stored["messages"][1]["citations"] == []
    assert stored["message_count"] == 2


def test_placeholder_message_is_deterministic(client) -> None:
    conversation_id = client.post("/api/v1/conversations", json={}).json()["id"]
    first = client.post(f"/api/v1/conversations/{conversation_id}/messages", json={"content": "你好"}).json()
    second = client.post(f"/api/v1/conversations/{conversation_id}/messages", json={"content": "你好"}).json()
    assert first["assistant_message"]["content"] == second["assistant_message"]["content"]


def test_blank_title_becomes_null(client) -> None:
    response = client.post("/api/v1/conversations", json={"title": "   "})
    assert response.status_code == 201
    assert response.json()["title"] is None


def test_message_to_unknown_conversation_returns_404(client) -> None:
    response = client.post("/api/v1/conversations/does-not-exist/messages", json={"content": "你好"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_blank_message_rejected(client) -> None:
    conversation_id = client.post("/api/v1/conversations", json={}).json()["id"]
    response = client.post(f"/api/v1/conversations/{conversation_id}/messages", json={"content": "   "})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# AI enabled (fake provider): grounded reply + strict citation validation
# ---------------------------------------------------------------------------


def test_ai_reply_stores_messages_and_validates_citations(client, app, db_session) -> None:
    ids = _create_memory(db_session)
    reply = {
        "content": "你那天写到她没回复消息、自己有点失落，我在这里听着。",
        "citations": [
            {"memory_id": ids["memory_id"], "snippet": "这是模型编造的片段"},
            {"memory_id": "fake-memory-id", "snippet": "不存在的来源"},
        ],
    }
    fake = FakeProvider([json.dumps(reply, ensure_ascii=False)])
    _enable_ai(app, fake)

    conversation_id = client.post("/api/v1/conversations", json={}).json()["id"]
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "她今天没有回复我的消息，我有点难过，你还记得吗？"},
    )
    assert response.status_code == 200, response.text
    data = response.json()

    assert data["mode"] == "ai"
    assert data["ai_used"] is True
    assert data["provider"] == "fake"
    assert data["model"] == "fake-model"
    assert data["assistant_message"]["content"] == reply["content"]
    assert data["assistant_message"]["model_name"] == "fake-model"
    assert data["assistant_message"]["mode"] == "ai"

    # Strict citation validation: only the real memory survives and all fields
    # are canonicalised from the database (the model snippet is discarded).
    assert len(data["citations"]) == 1
    citation = data["citations"][0]
    assert citation["memory_id"] == ids["memory_id"]
    assert citation["entry_id"] == ids["entry_id"]
    assert citation["revision_id"] == ids["revision_id"]
    assert citation["version"] == 1
    assert citation["snippet"] == "她今天没有回复我的消息"
    assert citation["memory_type"] == "subjective_feeling"
    assert citation["source_created_at"]
    assert all(item["memory_id"] != "fake-memory-id" for item in data["citations"])
    assert data["citations"] == data["assistant_message"]["citations"]

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["purpose"] == "companion"
    assert call["json_mode"] is True
    assert "untrusted" in call["system_prompt"].lower()
    assert "第三方读心" in call["system_prompt"]
    payload = _user_payload(call["user_prompt"])
    assert payload["user_message"].startswith("她今天没有回复我的消息")
    assert len(payload["context_memories"]) == 1
    context = payload["context_memories"][0]
    assert context["memory_id"] == ids["memory_id"]
    assert context["source"]["entry_id"] == ids["entry_id"]
    assert context["source"]["revision_id"] == ids["revision_id"]
    assert context["source"]["version"] == 1
    assert context["source"]["evidence"] == "她今天没有回复我的消息"
    assert payload["history"] == []

    # Both messages persisted with the AI mode on the assistant message.
    detail = client.get(f"/api/v1/conversations/{conversation_id}").json()
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]
    stored_assistant = detail["messages"][1]
    assert stored_assistant["mode"] == "ai"
    assert stored_assistant["model_name"] == "fake-model"
    assert len(stored_assistant["citations"]) == 1


def test_ai_reply_without_citations_is_allowed(client, app, db_session) -> None:
    _create_memory(db_session)
    fake = FakeProvider([json.dumps({"content": "我在听。", "citations": []})])
    _enable_ai(app, fake)
    conversation_id = client.post("/api/v1/conversations", json={}).json()["id"]
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "今天随便聊聊"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "ai"
    assert response.json()["citations"] == []


def test_ai_history_is_passed_for_follow_up_messages(client, app, db_session) -> None:
    _create_memory(db_session)
    fake = FakeProvider(
        [
            json.dumps({"content": "我听到了。", "citations": []}, ensure_ascii=False),
            json.dumps({"content": "我还记得。", "citations": []}, ensure_ascii=False),
        ]
    )
    _enable_ai(app, fake)
    conversation_id = client.post("/api/v1/conversations", json={}).json()["id"]

    first = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "她今天没有回复我的消息，我有点难过。"},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "我还是有点难过。"},
    )
    assert second.status_code == 200, second.text

    second_payload = _user_payload(fake.calls[1]["user_prompt"])
    history = second_payload["history"]
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert history[0]["content"] == "她今天没有回复我的消息，我有点难过。"
    assert history[1]["content"] == "我听到了。"


def test_invalid_provider_output_returns_502_and_stores_no_assistant(client, app, db_session) -> None:
    _create_memory(db_session)
    fake = FakeProvider(["这不是 JSON"])
    _enable_ai(app, fake)
    conversation_id = client.post("/api/v1/conversations", json={}).json()["id"]

    response = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "她今天没有回复我的消息，我有点难过。"},
    )
    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "ai_invalid_response"
    assert "这不是 JSON" not in response.text

    # The user's own message is not lost, but no assistant reply is fabricated.
    detail = client.get(f"/api/v1/conversations/{conversation_id}").json()
    assert [message["role"] for message in detail["messages"]] == ["user"]


def test_relevant_context_excludes_revoked_memories(client, app, db_session) -> None:
    revoked = _create_memory(
        db_session,
        memory_content="她今天没有回复我的消息",
        evidence_text="她今天没有回复我的消息",
        verification_status="revoked",
        is_active=False,
    )
    fake = FakeProvider([json.dumps({"content": "我在。", "citations": []})])
    _enable_ai(app, fake)
    conversation_id = client.post("/api/v1/conversations", json={}).json()["id"]
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "她今天没有回复我的消息，我有点难过。"},
    )
    assert response.status_code == 200, response.text
    payload = _user_payload(fake.calls[0]["user_prompt"])
    sent_ids = {item["memory_id"] for item in payload["context_memories"]}
    assert revoked["memory_id"] not in sent_ids
