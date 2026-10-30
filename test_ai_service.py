"""Offline deterministic tests for the AI adapter, prompts and disabled paths.

No test here opens a network socket: the OpenAI-compatible adapter is exercised
through ``httpx.MockTransport`` and every other case injects a fake provider.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import AIConfigurationError, AIResponseError, AIServiceError
from app.models.enums import MemoryType
from app.schemas.ai import ExtractionEnvelope
from app.services.ai_providers import (
    MockProvider,
    OpenAICompatibleProvider,
    build_provider,
)
from app.services.ai_service import (
    PROMPTS_DIR,
    AIService,
    build_ai_service,
    load_prompt,
    parse_json_object,
    strip_markdown_fences,
)

DIARY_TEXT = "她今天没有回复我的消息，我有点失落。"
VALID_CANDIDATE = {
    "content": "她今天没有回复消息",
    "memory_type": "explicit_fact",
    "confidence": 0.9,
    "evidence_text": "她今天没有回复我的消息",
}
VALID_ENVELOPE = {
    "candidates": [VALID_CANDIDATE],
    "provider": "claimed-provider",
    "model": "claimed-model",
    "mode": "ai",
}


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": "sqlite://",
        "data_dir": Path("data"),
        "backup_dir": Path("backups"),
        "ai_enabled": True,
        "ai_consent": True,
        "ai_provider": "openai_compatible",
        "ai_base_url": "https://provider.test/v1",
        "ai_model": "test-model",
        "ai_api_key": "sk-test-secret",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def _http_provider(
    content: str,
    *,
    status: int = 200,
    requests: list[httpx.Request] | None = None,
    raise_error: Exception | None = None,
) -> OpenAICompatibleProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if raise_error is not None:
            raise raise_error
        return httpx.Response(status, json={"choices": [{"message": {"content": content}}]})

    return OpenAICompatibleProvider(_settings(), transport=httpx.MockTransport(handler))


class _NeverCalledProvider:
    """Provider that fails the test if any disabled code path calls it."""

    name = "guard"
    model = "guard-model"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **kwargs: Any) -> str:  # pragma: no cover - must never run
        self.calls += 1
        raise AssertionError("provider must not be called when AI is disabled")


# ---------------------------------------------------------------------------
# JSON parsing / fencing
# ---------------------------------------------------------------------------


def test_strip_markdown_fences_handles_json_fence() -> None:
    assert strip_markdown_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_markdown_fences('```\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_markdown_fences('{"a": 1}') == '{"a": 1}'


def test_parse_json_object_accepts_fenced_and_prefixed_output() -> None:
    assert parse_json_object('```json\n{"ok": true}\n```') == {"ok": True}
    assert parse_json_object('Here is the JSON:\n```json\n{"ok": true}\n```\nDone.') == {"ok": True}
    assert parse_json_object('prefix {"ok": true} suffix') == {"ok": True}


@pytest.mark.parametrize(
    "raw",
    ["not json at all", '{"candidates": [', "```json\nnot json\n```", "", "   "],
)
def test_parse_json_object_rejects_malformed(raw: str) -> None:
    with pytest.raises(AIResponseError):
        parse_json_object(raw)


# ---------------------------------------------------------------------------
# OpenAI-compatible adapter
# ---------------------------------------------------------------------------


def test_extract_memories_parses_fenced_json_and_overrides_identity() -> None:
    requests: list[httpx.Request] = []
    content = "```json\n" + json.dumps(VALID_ENVELOPE, ensure_ascii=False) + "\n```"
    service = AIService(_settings(), provider=_http_provider(content, requests=requests))

    envelope = _run(
        service.extract_memories(
            entry_id="entry-1",
            revision_id="revision-1",
            version=1,
            content=DIARY_TEXT,
        )
    )

    assert isinstance(envelope, ExtractionEnvelope)
    assert envelope.mode == "ai"
    # Identity is our truth, not what the model claimed.
    assert envelope.provider == "openai_compatible"
    assert envelope.model == "test-model"
    assert len(envelope.candidates) == 1
    assert envelope.candidates[0].memory_type is MemoryType.EXPLICIT_FACT
    assert envelope.candidates[0].evidence_text == "她今天没有回复我的消息"

    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://provider.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test-secret"
    payload = json.loads(request.content)
    assert payload["model"] == "test-model"
    assert payload["max_tokens"] == 2000
    assert payload["response_format"] == {"type": "json_object"}
    assert [message["role"] for message in payload["messages"]] == ["system", "user"]
    # System prompt contains the injection-defence rules; user prompt marks data.
    assert "untrusted" in payload["messages"][0]["content"].lower()
    assert "<<<BEGIN_UNTRUSTED_DATA>>>" in payload["messages"][1]["content"]
    assert DIARY_TEXT in payload["messages"][1]["content"]


def test_extract_memories_rejects_unknown_candidate_field() -> None:
    payload = {
        "candidates": [{**VALID_CANDIDATE, "sql": "DROP TABLE diaries;"}],
        "provider": "p",
        "model": "m",
        "mode": "ai",
    }
    service = AIService(_settings(), provider=_http_provider(json.dumps(payload)))
    with pytest.raises(AIResponseError) as excinfo:
        _run(service.extract_memories(entry_id="e", revision_id="r", version=1, content=DIARY_TEXT))
    assert excinfo.value.code == "ai_invalid_response"


def test_extract_memories_rejects_unknown_top_level_field() -> None:
    payload = {**VALID_ENVELOPE, "raw_sql": "SELECT 1"}
    service = AIService(_settings(), provider=_http_provider(json.dumps(payload)))
    with pytest.raises(AIResponseError):
        _run(service.extract_memories(entry_id="e", revision_id="r", version=1, content=DIARY_TEXT))


def test_extract_memories_rejects_invalid_memory_type() -> None:
    payload = {
        "candidates": [{**VALID_CANDIDATE, "memory_type": "confirmed_fact"}],
        "provider": "p",
        "model": "m",
        "mode": "ai",
    }
    service = AIService(_settings(), provider=_http_provider(json.dumps(payload)))
    with pytest.raises(AIResponseError):
        _run(service.extract_memories(entry_id="e", revision_id="r", version=1, content=DIARY_TEXT))


def test_extract_memories_allows_empty_candidates() -> None:
    payload = {"candidates": [], "provider": "p", "model": "m", "mode": "ai"}
    service = AIService(_settings(), provider=_http_provider(json.dumps(payload)))
    envelope = _run(service.extract_memories(entry_id="e", revision_id="r", version=1, content=DIARY_TEXT))
    assert envelope.candidates == []


def test_http_error_maps_to_ai_service_error_without_leaking() -> None:
    service = AIService(_settings(), provider=_http_provider("", status=500))
    with pytest.raises(AIServiceError) as excinfo:
        _run(service.extract_memories(entry_id="e", revision_id="r", version=1, content=DIARY_TEXT))
    error = excinfo.value
    assert error.status_code == 502
    assert error.code == "ai_service_error"
    assert "sk-test-secret" not in str(error)
    assert DIARY_TEXT not in str(error)


def test_timeout_and_connection_errors_map_to_ai_service_error() -> None:
    for error in (
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("connection refused"),
    ):
        service = AIService(_settings(), provider=_http_provider("", raise_error=error))
        with pytest.raises(AIServiceError):
            _run(service.extract_memories(entry_id="e", revision_id="r", version=1, content=DIARY_TEXT))


def test_http_body_without_choices_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    provider = OpenAICompatibleProvider(_settings(), transport=httpx.MockTransport(handler))
    service = AIService(_settings(), provider=provider)
    with pytest.raises(AIResponseError):
        _run(service.extract_memories(entry_id="e", revision_id="r", version=1, content=DIARY_TEXT))


# ---------------------------------------------------------------------------
# Companion / narrative parsing
# ---------------------------------------------------------------------------


def test_companion_reply_parses_content_and_citations() -> None:
    reply_payload = {
        "content": "你那天写到有点失落，我在这里听你说。",
        "citations": [{"memory_id": "memory-1"}],
    }
    requests: list[httpx.Request] = []
    service = AIService(
        _settings(),
        provider=_http_provider(json.dumps(reply_payload, ensure_ascii=False), requests=requests),
    )
    reply = _run(
        service.companion_reply(
            user_message="你还记得我上周的心情吗？",
            context_memories=[
                {"memory_id": "memory-1", "content": "我有点失落", "source": {"entry_id": "e1"}}
            ],
            history=[{"role": "user", "content": "你好"}],
        )
    )
    assert reply.mode == "ai"
    assert reply.content == reply_payload["content"]
    assert reply.citations == [{"memory_id": "memory-1"}]
    payload = json.loads(requests[0].content)
    assert payload["response_format"] == {"type": "json_object"}
    assert "untrusted" in payload["messages"][0]["content"].lower()
    assert "memory-1" in payload["messages"][1]["content"]


@pytest.mark.parametrize(
    "payload",
    [
        {"content": "", "citations": []},
        {"citations": []},
        {"content": "hi", "citations": "not-a-list"},
        {"content": "hi", "citations": [], "sql": "DROP TABLE memories"},
    ],
)
def test_companion_reply_rejects_invalid_payloads(payload: dict[str, Any]) -> None:
    service = AIService(_settings(), provider=_http_provider(json.dumps(payload, ensure_ascii=False)))
    with pytest.raises(AIResponseError):
        _run(service.companion_reply(user_message="hello", context_memories=[], history=[]))


def test_generate_narrative_strips_fences_and_rejects_empty() -> None:
    service = AIService(_settings(), provider=_http_provider("```\n本周你写了两篇日记。\n```"))
    narrative = _run(service.generate_narrative(purpose="weekly_review", facts={"counts": {"diaries": 2}}))
    assert narrative == "本周你写了两篇日记。"

    empty_service = AIService(_settings(), provider=_http_provider("   "))
    with pytest.raises(AIResponseError):
        _run(empty_service.generate_narrative(purpose="weekly_review", facts={"counts": {"diaries": 0}}))


# ---------------------------------------------------------------------------
# Disabled / unconfigured behaviour: must raise and never call the provider
# ---------------------------------------------------------------------------


def test_disabled_service_raises_without_calling_provider() -> None:
    settings = _settings(ai_enabled=False, ai_consent=False, ai_provider="disabled", ai_api_key=None)
    guard = _NeverCalledProvider()
    service = AIService(settings, provider=guard)
    assert service.configured is False
    assert service.provider_name == "guard"
    assert service.model_name == "guard-model"

    with pytest.raises(AIConfigurationError):
        _run(service.extract_memories(entry_id="e", revision_id="r", version=1, content=DIARY_TEXT))
    with pytest.raises(AIConfigurationError):
        _run(service.companion_reply(user_message="hi", context_memories=[], history=[]))
    with pytest.raises(AIConfigurationError):
        _run(service.generate_narrative(purpose="weekly_review", facts={}))
    assert guard.calls == 0


def test_enabled_but_incomplete_configuration_raises_without_network() -> None:
    settings = _settings().model_copy(update={"ai_base_url": ""})
    guard = _NeverCalledProvider()
    service = AIService(settings, provider=guard)
    assert service.configured is False
    with pytest.raises(AIConfigurationError):
        _run(service.extract_memories(entry_id="e", revision_id="r", version=1, content=DIARY_TEXT))
    assert guard.calls == 0


def test_build_ai_service_disabled_has_no_provider() -> None:
    settings = _settings(ai_enabled=False, ai_consent=False, ai_provider="disabled", ai_api_key=None)
    service = build_ai_service(settings)
    assert service.provider is None
    assert service.configured is False
    assert service.provider_name == "disabled"
    health = _run(service.healthcheck())
    assert health["configured"] is False
    assert health["external_data_transfer"] is False


def test_build_ai_service_mock_provider_is_offline_and_honest() -> None:
    settings = _settings(ai_provider="mock", ai_base_url="", ai_model="mock-model", ai_api_key=None)
    service = build_ai_service(settings)
    assert service.configured is True
    assert service.provider_name == "mock"
    assert isinstance(service.provider, MockProvider)
    health = _run(service.healthcheck())
    assert health["configured"] is True
    assert health["external_data_transfer"] is False
    assert health["prompts_ready"] is True
    # A mock extraction is honest: it never invents memories.
    envelope = _run(service.extract_memories(entry_id="e", revision_id="r", version=1, content=DIARY_TEXT))
    assert envelope.candidates == []
    reply = _run(service.companion_reply(user_message="hi", context_memories=[], history=[]))
    assert reply.mode == "ai"
    assert "mock" in reply.content
    assert _run(service.generate_narrative(purpose="weekly_review", facts={}))


def test_build_ai_service_openai_provider_has_no_secret_in_health() -> None:
    settings = _settings()
    service = build_ai_service(settings)
    assert service.configured is True
    assert service.provider_name == "openai_compatible"
    assert service.model_name == "test-model"
    health = _run(service.healthcheck())
    assert health["configured"] is True
    assert health["external_data_transfer"] is True
    assert health["prompts_ready"] is True
    assert "sk-test-secret" not in json.dumps(health)


def test_build_provider_returns_none_when_not_configured() -> None:
    settings = _settings(ai_enabled=False, ai_consent=False, ai_provider="disabled", ai_api_key=None)
    assert build_provider(settings) is None


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------


def test_prompt_directory_contains_all_templates() -> None:
    for filename in ("memory_extraction.md", "companion.md", "weekly_review.md"):
        path = PROMPTS_DIR / filename
        assert path.is_file(), filename
        assert path.read_text(encoding="utf-8").strip()


def test_extraction_prompt_contains_required_safety_rules() -> None:
    prompt = load_prompt("extraction").lower()
    for expected in (
        "explicit_fact",
        "subjective_feeling",
        "explicit_belief",
        "model_inference",
        "evidence_text",
        "exact substring",
        "no evidence",
        "contradiction",
        "untrusted",
        "not instructions",
        "不得编造",
        "不得升级",
    ):
        assert expected in prompt, expected


def test_companion_prompt_contains_required_safety_rules() -> None:
    prompt = load_prompt("companion")
    for expected in (
        "untrusted",
        "第三方读心",
        "诊断",
        "absence of memory",
        "citations",
        "memory_id",
        "矛盾",
        "删除",
        "感受",
        "推断",
    ):
        assert expected in prompt, expected


def test_weekly_review_prompt_contains_required_rules() -> None:
    prompt = load_prompt("insight")
    for expected in (
        "untrusted",
        "facts",
        "不得编造",
        "不擅自调和",
        "诊断",
        "周回顾",
        "月回顾",
    ):
        assert expected in prompt, expected
