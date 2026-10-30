"""独立验收测试：覆盖用户需求中的 20 项行为（端到端、离线、临时数据库）。

本文件由 qa-dev 独立编写，不复用现有测试的断言实现；只复用 ``conftest.py``
提供的 ``settings`` / ``app`` / ``client`` fixtures。所有 AI 行为均使用进程内
fake，绝不访问网络；所有数据库均为 pytest ``tmp_path`` 下的临时 SQLite，
不会打开 ``data/heartlog.db``。

需求编号与测试的对应关系见每个测试的 docstring。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core.config import Settings, get_settings
from app.main import create_app
from app.models.audit import AuditEvent
from app.models.diary import DiaryEntry, DiaryRevision
from app.models.enums import MemoryType
from app.models.memory import Memory, MemorySource
from app.schemas.ai import CandidateMemory, ExtractionEnvelope
from app.services.memory_service import MemoryService

API = "/api/v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class FakeAI:
    """Deterministic in-process stand-in for the injected AI service."""

    configured = True
    provider_name = "qa-fake-provider"
    model_name = "qa-fake-model"

    def __init__(self, candidates: list[CandidateMemory]) -> None:
        self.candidates = candidates
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
            {"entry_id": entry_id, "revision_id": revision_id, "version": version, "content": content}
        )
        return ExtractionEnvelope(
            candidates=list(self.candidates),
            provider=self.provider_name,
            model=self.model_name,
        )


def _candidate(
    content: str,
    *,
    evidence: str,
    memory_type: MemoryType = MemoryType.EXPLICIT_FACT,
    confidence: float | None = 0.8,
    start_offset: int | None = None,
    end_offset: int | None = None,
) -> CandidateMemory:
    return CandidateMemory(
        content=content,
        memory_type=memory_type,
        confidence=confidence,
        evidence_text=evidence,
        start_offset=start_offset,
        end_offset=end_offset,
    )


def _install_ai(app: Any, candidates: list[CandidateMemory]) -> FakeAI:
    fake = FakeAI(candidates)
    app.state.ai_service = fake
    return fake


def _count(app: Any, model: type[Any]) -> int:
    with app.state.session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _audit_event_types(app: Any) -> list[str]:
    with app.state.session_factory() as session:
        return [row for row in session.scalars(select(AuditEvent.event_type))]


def _assert_error(response: Any, status_code: int, code: str) -> dict[str, Any]:
    assert response.status_code == status_code, response.text
    body = response.json()
    assert set(body) == {"error"}, body
    assert set(body["error"]) >= {"code", "message"}, body
    assert body["error"]["code"] == code, body
    return body["error"]


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        environment="test",
        database_url=f"sqlite:///{(tmp_path / 'qa-store.db').as_posix()}",
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "backups",
        auto_create_schema=True,
        api_token=None,
        admin_token="qa-admin-token",
        ai_enabled=False,
        ai_consent=False,
        ai_provider="disabled",
        _env_file=None,
    )
    values.update(overrides)
    return Settings(**values)


# ---------------------------------------------------------------------------
# 需求 1：健康检查
# ---------------------------------------------------------------------------
def test_requirement_01_health_check(client: TestClient) -> None:
    """需求 1：/health 返回真实状态（数据库 ok、AI 关闭、无需认证）。"""

    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["ai_enabled"] is False
    assert body["ai_ready"] is False
    assert body["environment"] == "test"
    assert body["app"] == "HeartLog"
    assert body["version"]

    root = client.get("/")
    assert root.status_code == 200
    assert root.json()["health"] == "/health"
    assert root.json()["api"] == API


# ---------------------------------------------------------------------------
# 需求 2/3/4：创建、原文精确保存、初始修订
# ---------------------------------------------------------------------------
def test_requirement_02_03_04_create_keeps_exact_original_and_initial_revision(
    app: Any, client: TestClient
) -> None:
    """需求 2/3/4：创建成功；原文逐字符保存（含空白/换行/emoji）；version=1 修订存在。"""

    original = "  第一行：她今天没有回复我。\n\n第二行：😔 我有点失落。\t \r\n"
    assert original.strip(), "test content must not be blank"
    response = client.post(f"{API}/diaries", json={"content": original, "metadata": {"mood": "低"}})
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["id"]
    assert body["original_text"] == original
    assert body["current_content"] == original
    assert body["current_version"] == 1
    assert body["current_revision"]["version"] == 1
    assert body["current_revision"]["content"] == original
    assert body["original_content_hash"] == hashlib.sha256(original.encode("utf-8")).hexdigest()
    assert body["metadata"] == {"mood": "低"}

    revisions = client.get(f"{API}/diaries/{body['id']}/revisions")
    assert revisions.status_code == 200
    assert revisions.json()["total"] == 1
    assert revisions.json()["versions"][0]["version"] == 1
    assert revisions.json()["versions"][0]["content"] == original

    integrity = client.get(f"{API}/diaries/{body['id']}/integrity").json()
    assert integrity["original_text_matches_hash"] is True
    assert integrity["revision_chain_valid"] is True
    assert integrity["revisions_checked"] == 1
    with app.state.session_factory() as session:
        stored = session.get(DiaryEntry, body["id"])
        assert stored is not None and stored.original_text == original


# ---------------------------------------------------------------------------
# 需求 5/6/7：编辑=新增修订、原永不覆盖、历史可读
# ---------------------------------------------------------------------------
def test_requirement_05_06_07_edit_appends_revision_and_never_overwrites_original(
    app: Any, client: TestClient
) -> None:
    """需求 5/6/7：PUT 追加修订；原文与旧修订不可变；所有历史版本可读取且哈希链完整。"""

    original = "原始第一版：这是不可被覆盖的原文。"
    created = client.post(f"{API}/diaries", json={"content": original}).json()
    diary_id = created["id"]
    version_one = created["current_revision"]

    second = "第二版：补充了一些细节。"
    updated = client.put(
        f"{API}/diaries/{diary_id}",
        json={"content": second, "expected_version": 1, "change_reason": "补充细节"},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["original_text"] == original
    assert body["current_content"] == second
    assert body["current_version"] == 2
    assert body["current_revision"]["version"] == 2
    assert body["current_revision"]["previous_revision_hash"] == version_one["content_hash"]
    assert body["current_revision"]["change_reason"] == "补充细节"

    third = "第三版：再次编辑。"
    assert (
        client.put(
            f"{API}/diaries/{diary_id}",
            json={"content": third, "expected_version": 2},
        ).status_code
        == 200
    )

    revisions = client.get(f"{API}/diaries/{diary_id}/revisions").json()
    assert revisions["total"] == 3
    assert [item["version"] for item in revisions["versions"]] == [1, 2, 3]
    assert revisions["versions"][0]["content"] == original
    assert revisions["versions"][1]["content"] == second
    assert revisions["versions"][2]["content"] == third

    for version, expected in ((1, original), (2, second), (3, third)):
        single = client.get(f"{API}/diaries/{diary_id}/revisions/{version}")
        assert single.status_code == 200
        assert single.json()["content"] == expected

    integrity = client.get(f"{API}/diaries/{diary_id}/integrity").json()
    assert integrity["revision_chain_valid"] is True
    assert integrity["revisions_checked"] == 3
    with app.state.session_factory() as session:
        entry = session.get(DiaryEntry, diary_id)
        assert entry is not None
        assert entry.original_text == original
        assert entry.current_version == 3


# ---------------------------------------------------------------------------
# 需求 9：不存在 -> 404
# ---------------------------------------------------------------------------
def test_requirement_09_missing_resources_return_404(client: TestClient) -> None:
    """需求 9：不存在的日记/修订/记忆返回 404（不是 500，也不泄漏堆栈）。"""

    for path in (
        f"{API}/diaries/does-not-exist",
        f"{API}/diaries/does-not-exist/revisions",
        f"{API}/diaries/does-not-exist/revisions/1",
        f"{API}/diaries/does-not-exist/integrity",
    ):
        error = _assert_error(client.get(path), 404, "not_found")
        assert "Traceback" not in json.dumps(error)

    _assert_error(client.put(f"{API}/diaries/does-not-exist", json={"content": "x"}), 404, "not_found")
    _assert_error(client.delete(f"{API}/diaries/does-not-exist"), 404, "not_found")
    _assert_error(client.post(f"{API}/diaries/does-not-exist/restore"), 404, "not_found")

    for path in (
        f"{API}/memories/does-not-exist",
        f"{API}/memories/does-not-exist/sources",
    ):
        _assert_error(client.get(path), 404, "not_found")
    _assert_error(client.post(f"{API}/memories/does-not-exist/confirm"), 404, "not_found")
    _assert_error(client.post(f"{API}/memories/does-not-exist/revoke"), 404, "not_found")
    _assert_error(
        client.post(f"{API}/memories/does-not-exist/correct", json={"content": "改"}),
        404,
        "not_found",
    )


# ---------------------------------------------------------------------------
# 需求 10：非法输入 -> 合适的 4xx
# ---------------------------------------------------------------------------
def test_requirement_10_invalid_input_returns_4xx_without_echo(client: TestClient) -> None:
    """需求 10：非法输入返回 4xx；错误响应不回显原始内容（隐私）。"""

    secret = "绝密内容不应出现在错误响应中-QA-123"
    invalid_posts: list[Any] = [
        {"content": ""},
        {"content": "   \n\t  "},
        # QA 回归：NUL/不可见控制字符曾被数据库 CHECK 变成 500，现在必须是 422。
        {"content": "\u0000"},
        {"content": "正常\x01控制字符"},
        {},
        {"content": "正常", "unexpected_field": secret},
        {"content": secret + "字" * 50_000},
        {"content": "正常", "metadata": {"blob": secret * 100}},
        "not-an-object",
    ]
    for payload in invalid_posts:
        response = client.post(f"{API}/diaries", json=payload)
        assert 400 <= response.status_code < 500, (payload, response.status_code, response.text)
        assert secret not in response.text
        assert "Traceback" not in response.text

    created = client.post(f"{API}/diaries", json={"content": "非法编辑输入"}).json()
    diary_id = created["id"]
    for payload in (
        {"content": ""},
        {"content": "x", "expected_version": 0},
        {"content": "x", "expected_version": "not-an-int"},
        {"content": "x", "bogus": secret},
    ):
        response = client.put(f"{API}/diaries/{diary_id}", json=payload)
        assert 400 <= response.status_code < 500
        assert secret not in response.text

    for params in (
        {"limit": 0},
        {"limit": 10_000},
        {"offset": -1},
        {"date_from": "not-a-date"},
    ):
        response = client.get(f"{API}/diaries", params=params)
        assert 400 <= response.status_code < 500, (params, response.text)

    # 检索参数同样必须被校验，而不是 500。
    assert client.get(f"{API}/search").status_code == 422
    assert client.get(f"{API}/search", params={"q": " "}).status_code == 422
    assert client.get(f"{API}/search", params={"q": "x", "scope": "invalid"}).status_code == 422
    assert client.get(f"{API}/memories", params={"memory_type": "invalid"}).status_code == 422
    assert client.get(f"{API}/memories", params={"verification_status": "invalid"}).status_code == 422


# ---------------------------------------------------------------------------
# 需求 11/12：软删除隐藏、修订保留
# ---------------------------------------------------------------------------
def test_requirement_11_12_soft_delete_hides_entry_but_keeps_revisions(app: Any, client: TestClient) -> None:
    """需求 11/12：软删除后默认列表/详情/搜索隐藏；数据库仍保留 entry 与全部修订。"""

    original = "软删除验收原文：这件事我想先藏起来。"
    created = client.post(f"{API}/diaries", json={"content": original}).json()
    diary_id = created["id"]
    second = "软删除验收第二版内容。"
    assert client.put(f"{API}/diaries/{diary_id}", json={"content": second}).status_code == 200

    assert client.delete(f"{API}/diaries/{diary_id}").status_code == 200
    assert client.get(f"{API}/diaries").json()["total"] == 0
    _assert_error(client.get(f"{API}/diaries/{diary_id}"), 404, "not_found")
    _assert_error(client.get(f"{API}/diaries/{diary_id}/revisions"), 404, "not_found")
    searched = client.get(f"{API}/search", params={"q": "藏起来", "scope": "diaries"})
    assert searched.status_code == 200 and searched.json()["total"] == 0

    with app.state.session_factory() as session:
        entry = session.get(DiaryEntry, diary_id)
        assert entry is not None and entry.deleted_at is not None
        revisions = list(session.scalars(select(DiaryRevision).where(DiaryRevision.entry_id == diary_id)))
        assert len(revisions) == 2, "软删除不得删除修订历史"
        assert {revision.content for revision in revisions} == {original, second}

    _assert_error(client.delete(f"{API}/diaries/{diary_id}"), 404, "not_found")

    restored = client.post(f"{API}/diaries/{diary_id}/restore")
    assert restored.status_code == 200
    assert restored.json()["deleted_at"] is None
    assert client.get(f"{API}/diaries").json()["total"] == 1
    _assert_error(client.post(f"{API}/diaries/{diary_id}/restore"), 409, "not_deleted")


# ---------------------------------------------------------------------------
# 需求 8：重启后数据仍在
# ---------------------------------------------------------------------------
def test_requirement_08_data_persists_across_app_restart(tmp_path: Path) -> None:
    """需求 8：应用重启（新引擎/新进程内 app）后日记、历史修订、记忆仍在。"""

    settings = _settings(tmp_path)
    first_text = "重启验收：原文第一版，包含可被引用的证据句子。"
    evidence = "包含可被引用的证据句子"
    app1 = create_app(settings)
    try:
        with TestClient(app1) as client:
            created = client.post(f"{API}/diaries", json={"content": first_text}).json()
            diary_id = created["id"]
            revision_one_id = created["current_revision_id"]
            assert (
                client.put(f"{API}/diaries/{diary_id}", json={"content": "重启验收第二版"}).status_code == 200
            )
            _install_ai(
                app1,
                [_candidate("重启验收记忆：日记里有可引用的证据", evidence=evidence)],
            )
            extracted = client.post(
                f"{API}/memories/extract",
                json={"entry_id": diary_id, "revision_id": revision_one_id},
            )
            assert extracted.status_code == 200, extracted.text
            memory_id = extracted.json()["created"][0]["id"]
            assert client.post(f"{API}/memories/{memory_id}/confirm").status_code == 200
    finally:
        app1.state.engine.dispose()

    app2 = create_app(settings)
    try:
        with TestClient(app2) as client:
            detail = client.get(f"{API}/diaries/{diary_id}")
            assert detail.status_code == 200, detail.text
            assert detail.json()["original_text"] == first_text
            assert detail.json()["current_version"] == 2
            revisions = client.get(f"{API}/diaries/{diary_id}/revisions").json()
            assert [item["version"] for item in revisions["versions"]] == [1, 2]
            assert revisions["versions"][0]["content"] == first_text

            memories = client.get(f"{API}/memories").json()
            assert memories["total"] == 1
            memory = memories["items"][0]
            assert memory["id"] == memory_id
            assert memory["verification_status"] == "confirmed"
            assert memory["source_revision_id"] == revision_one_id
            assert memory["source_version"] == 1
            found = client.get(f"{API}/search", params={"q": "可引用的证据", "scope": "memories"})
            assert found.status_code == 200 and found.json()["total"] == 1
    finally:
        app2.state.engine.dispose()


# ---------------------------------------------------------------------------
# 需求 13/14/15/18：记忆必须可溯源、待确认、不得自动成为事实、伪造来源被拒
# ---------------------------------------------------------------------------
def test_requirement_13_14_15_18_memory_requires_source_and_stays_pending(
    app: Any, client: TestClient
) -> None:
    """需求 13/14/15/18：无来源/伪造来源不落库；pending 不自动确认；指向正确 entry+revision。"""

    first_text = "记忆验收原文第一版：她说周末会来找我。"
    second_text = "记忆验收原文第二版：内容已编辑。"
    created = client.post(f"{API}/diaries", json={"content": first_text}).json()
    diary_id = created["id"]
    revision_one_id = created["current_revision_id"]
    assert client.put(f"{API}/diaries/{diary_id}", json={"content": second_text}).status_code == 200

    evidence = "她说周末会来找我"
    start = first_text.index(evidence)
    valid = _candidate(
        "模型推断：对方可能重视周末的约定",
        evidence=evidence,
        memory_type=MemoryType.MODEL_INFERENCE,
        confidence=0.99,
        start_offset=start,
        end_offset=start + len(evidence),
    )
    fabricated = _candidate(
        "模型编造：她其实已经和别人约好了",
        evidence="这句话完全不存在于日记原文中",
        memory_type=MemoryType.EXPLICIT_FACT,
    )
    bad_offsets = _candidate(
        "偏移错误的候选",
        evidence=first_text[1:5],
        memory_type=MemoryType.EXPLICIT_FACT,
        start_offset=0,
        end_offset=len(first_text[1:5]),
    )

    _install_ai(app, [valid, fabricated, bad_offsets])
    response = client.post(
        f"{API}/memories/extract",
        json={"entry_id": diary_id, "version": 1},
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["entry_id"] == diary_id
    assert report["revision_id"] == revision_one_id
    assert report["version"] == 1
    assert report["provider"] == "qa-fake-provider"
    assert report["candidate_count"] == 3
    assert report["created_count"] == 1
    assert report["rejected_count"] == 2
    assert {item["reason"] for item in report["rejected"]} == {
        "evidence_not_found",
        "evidence_offset_mismatch",
    }
    for rejected in report["rejected"]:
        assert "这句话完全不存在" not in json.dumps(rejected, ensure_ascii=False)
        assert "模型编造" not in json.dumps(rejected, ensure_ascii=False)

    memory = report["created"][0]
    assert memory["verification_status"] == "pending"
    assert memory["created_by"] == "model"
    assert memory["memory_type"] == "model_inference"
    assert memory["confidence"] == 0.99
    assert memory["source_entry_id"] == diary_id
    assert memory["source_revision_id"] == revision_one_id
    assert memory["source_version"] == 1
    assert memory["sources"][0]["evidence_text"] == evidence
    assert memory["sources"][0]["revision_id"] == revision_one_id

    sources = client.get(f"{API}/memories/{memory['id']}/sources")
    assert sources.status_code == 200
    assert sources.json()["total"] == 1
    source = sources.json()["sources"][0]
    assert source["revision_id"] == revision_one_id
    assert source["revision_version"] == 1
    assert first_text[source["start_offset"] : source["end_offset"]] == source["evidence_text"]

    # 模型置信度 0.99 也不等于确认；确认动作必须由用户显式发起。
    listed = client.get(f"{API}/memories").json()
    assert listed["total"] == 1
    assert listed["items"][0]["verification_status"] == "pending"

    confirmed = client.post(f"{API}/memories/{memory['id']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["verification_status"] == "confirmed"
    assert confirmed.json()["confirmed_by"] == "user"
    assert confirmed.json()["memory_type"] == "model_inference", "确认不得改变推断类型"
    assert confirmed.json()["created_by"] == "model"

    # 全部候选都是伪造来源 -> 422，且一行都不写入。
    _install_ai(app, [fabricated])
    rejected_all = client.post(
        f"{API}/memories/extract",
        json={"entry_id": diary_id, "version": 1},
    )
    _assert_error(rejected_all, 422, "no_valid_candidates")
    assert _count(app, Memory) == 1
    assert _count(app, MemorySource) == 1


# ---------------------------------------------------------------------------
# 需求 13：正式记忆中不存在"无来源"入口（API + 数据库层）
# ---------------------------------------------------------------------------
def test_requirement_13_no_sourceless_memory_can_enter_formal_store(app: Any, client: TestClient) -> None:
    """需求 13：没有创建无来源记忆的 API；数据库也拒绝无主来源的记忆行。"""

    assert client.post(f"{API}/memories", json={"content": "没有来源的记忆"}).status_code == 405
    assert client.put(f"{API}/memories/x", json={}).status_code == 405
    # The new forgetting endpoint only deletes existing memories and requires
    # explicit confirmation; it is not a source-less creation path.
    assert client.delete(f"{API}/memories/x").status_code == 422
    assert client.delete(f"{API}/memories/x", headers={"X-Confirm-Memory-Delete": "true"}).status_code == 404

    with app.state.session_factory() as session:
        session.add(
            Memory(
                content="无来源的断言",
                memory_type="explicit_fact",
                source_type="diary_entry",
                source_entry_id=None,
                source_revision_id=None,
                verification_status="pending",
                is_active=True,
                created_by="model",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    assert _count(app, Memory) == 0


# ---------------------------------------------------------------------------
# AI 关闭：诚实 503/placeholder，且不构造网络客户端
# ---------------------------------------------------------------------------
def test_ai_disabled_is_honest_and_never_uses_network(
    app: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AI 关闭时：extract=503 且零写入；对话=占位；回顾=本地统计；绝不构造网络客户端。"""

    import httpx

    def _bomb_async_client(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("AI disabled: no network client may be constructed")

    monkeypatch.setattr(httpx, "AsyncClient", _bomb_async_client)

    assert app.state.ai_service.provider is None
    assert app.state.ai_service.configured is False

    created = client.post(f"{API}/diaries", json={"content": "AI 关闭时的日记内容"}).json()
    diary_id = created["id"]
    before = (_count(app, Memory), _count(app, MemorySource), _count(app, AuditEvent))

    extraction = client.post(f"{API}/memories/extract", json={"entry_id": diary_id})
    _assert_error(extraction, 503, "ai_not_configured")
    after = (_count(app, Memory), _count(app, MemorySource), _count(app, AuditEvent))
    assert after == before, "AI 关闭时提取不得留下任何写入（含审计）"

    conversation = client.post(f"{API}/conversations", json={"title": "离线对话"})
    assert conversation.status_code == 201
    conversation_id = conversation.json()["id"]
    exchange = client.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"content": "我今天有点难过。"},
    )
    assert exchange.status_code == 200
    payload = exchange.json()
    assert payload["mode"] == "placeholder"
    assert payload["ai_used"] is False
    assert payload["citations"] == []
    assert payload["notice"]
    assert "placeholder" in payload["assistant_message"]["content"].lower()
    stored = client.get(f"{API}/conversations/{conversation_id}").json()
    assert [message["role"] for message in stored["messages"]] == ["user", "assistant"]

    weekly = client.get(f"{API}/insights/weekly")
    assert weekly.status_code == 200, weekly.text
    weekly_body = weekly.json()
    assert weekly_body["mode"] == "local_statistics"
    assert weekly_body["ai_used"] is False
    assert weekly_body["narrative"] is None
    assert weekly_body["statistics"]["diary_entries"] >= 1
    assert weekly_body["notes"]
    assert weekly_body["disclaimer"]

    monthly = client.get(f"{API}/insights/monthly")
    assert monthly.status_code == 200
    assert monthly.json()["mode"] == "local_statistics"
    assert monthly.json()["ai_used"] is False


# ---------------------------------------------------------------------------
# 需求 16：撤回后从默认列表/搜索消失
# ---------------------------------------------------------------------------
def test_requirement_16_revoked_memory_hidden_from_normal_list_and_search(
    app: Any, client: TestClient
) -> None:
    """需求 16：撤回的记忆不出现在默认列表/搜索，但仍可按 id 追溯；重复撤回/确认返回 409。"""

    created = client.post(f"{API}/diaries", json={"content": "撤回验收：我周末也许想去海边走走。"}).json()
    evidence = "我周末也许想去海边走走"
    _install_ai(
        app,
        [_candidate("周末可能想去海边", evidence=evidence, memory_type=MemoryType.SUBJECTIVE_FEELING)],
    )
    extracted = client.post(f"{API}/memories/extract", json={"entry_id": created["id"]})
    assert extracted.status_code == 200, extracted.text
    memory_id = extracted.json()["created"][0]["id"]

    found = client.get(f"{API}/search", params={"q": "海边", "scope": "memories"}).json()
    assert found["total"] == 1 and found["items"][0]["memory_id"] == memory_id

    revoked = client.post(f"{API}/memories/{memory_id}/revoke", json={"reason": "不准确"})
    assert revoked.status_code == 200
    assert revoked.json()["verification_status"] == "revoked"
    assert revoked.json()["is_active"] is False

    assert client.get(f"{API}/memories").json()["total"] == 0
    assert client.get(f"{API}/search", params={"q": "海边", "scope": "memories"}).json()["total"] == 0
    hidden = client.get(f"{API}/search", params={"q": "海边"}).json()
    assert all(hit.get("memory_id") != memory_id for hit in hidden["items"])

    detail = client.get(f"{API}/memories/{memory_id}")
    assert detail.status_code == 200
    assert detail.json()["verification_status"] == "revoked"
    revoked_list = client.get(f"{API}/memories", params={"verification_status": "revoked"}).json()
    assert revoked_list["total"] == 1
    _assert_error(client.post(f"{API}/memories/{memory_id}/revoke"), 409, "already_revoked")
    _assert_error(client.post(f"{API}/memories/{memory_id}/confirm"), 409, "invalid_state_transition")


# ---------------------------------------------------------------------------
# 需求 17：矛盾来源不得自动合并
# ---------------------------------------------------------------------------
def test_requirement_17_contradictory_sources_are_not_merged(app: Any, client: TestClient) -> None:
    """需求 17：互相矛盾的日记会得到两条独立记忆，不合并、不覆盖、都可追溯。"""

    first = client.post(f"{API}/diaries", json={"content": "她说周末会来找我。"}).json()
    second = client.post(f"{API}/diaries", json={"content": "她说周末不会来找我。"}).json()

    _install_ai(app, [_candidate("她说周末会来", evidence="她说周末会来找我")])
    first_report = client.post(f"{API}/memories/extract", json={"entry_id": first["id"]})
    assert first_report.status_code == 200, first_report.text
    first_memory = first_report.json()["created"][0]

    _install_ai(app, [_candidate("她说周末不会来", evidence="她说周末不会来找我")])
    second_report = client.post(f"{API}/memories/extract", json={"entry_id": second["id"]})
    assert second_report.status_code == 200, second_report.text
    second_memory = second_report.json()["created"][0]

    assert first_memory["id"] != second_memory["id"]
    assert first_memory["source_entry_id"] == first["id"]
    assert second_memory["source_entry_id"] == second["id"]
    assert client.get(f"{API}/memories").json()["total"] == 2
    hits = client.get(f"{API}/search", params={"q": "周末", "scope": "memories"}).json()
    assert hits["total"] == 2
    assert {hit["memory_id"] for hit in hits["items"]} == {first_memory["id"], second_memory["id"]}
    assert len({hit["snippet"] for hit in hits["items"]}) == 2


# ---------------------------------------------------------------------------
# 需求 19：关键写入失败 -> 整个事务回滚
# ---------------------------------------------------------------------------
def test_requirement_19_failed_critical_write_rolls_back_entire_batch(
    app: Any, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """需求 19：批量写记忆时第二次构建失败，则不留下任何 memory/source/审计行。"""

    text = "回滚验收原文：她说周末会来找我，我有点开心。"
    created = client.post(f"{API}/diaries", json={"content": text}).json()
    diary_id = created["id"]
    first_evidence = "她说周末会来找我"
    second_evidence = "我有点开心"
    _install_ai(
        app,
        [
            _candidate("终于要见面了", evidence=first_evidence),
            _candidate("心情不错", evidence=second_evidence),
        ],
    )

    original_build = MemoryService._build_memory
    calls = {"n": 0}

    def failing_build(self: MemoryService, *args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise SQLAlchemyError("simulated critical write failure")
        return original_build(self, *args, **kwargs)

    monkeypatch.setattr(MemoryService, "_build_memory", failing_build)

    response = client.post(f"{API}/memories/extract", json={"entry_id": diary_id})
    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "database_error"

    assert _count(app, Memory) == 0
    assert _count(app, MemorySource) == 0
    event_types = _audit_event_types(app)
    assert "memory.created" not in event_types
    assert "memories.extracted" not in event_types
    assert "diary.created" in event_types


# ---------------------------------------------------------------------------
# 需求 20：并发编辑不静默丢失修订
# ---------------------------------------------------------------------------
def test_requirement_20_concurrent_edits_do_not_silently_lose_revisions(app: Any, client: TestClient) -> None:
    """需求 20：两个基于 version=1 的并发编辑，一个 200 一个 409，无静默覆盖；重试后修订齐全。"""

    created = client.post(f"{API}/diaries", json={"content": "并发验收起点"}).json()
    diary_id = created["id"]
    barrier = threading.Barrier(2)
    results: list[tuple[int, dict[str, Any]]] = []
    lock = threading.Lock()

    def worker(content: str) -> None:
        barrier.wait(timeout=10)
        response = client.put(
            f"{API}/diaries/{diary_id}",
            json={"content": content, "expected_version": 1},
        )
        with lock:
            results.append((response.status_code, response.json()))

    threads = [threading.Thread(target=worker, args=(f"并发写入-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(results) == 2, results
    statuses = sorted(status for status, _ in results)
    assert statuses == [200, 409], results
    conflict = next(body for status, body in results if status == 409)
    assert conflict["error"]["code"] == "version_conflict"

    revisions = client.get(f"{API}/diaries/{diary_id}/revisions").json()
    assert [item["version"] for item in revisions["versions"]] == [1, 2]
    with app.state.session_factory() as session:
        assert session.get(DiaryEntry, diary_id).current_version == 2

    # 冲突的一方重新基于最新版本提交后，所有修订都在，不被静默丢弃。
    retry = client.put(
        f"{API}/diaries/{diary_id}",
        json={"content": "冲突后重试成功", "expected_version": 2},
    )
    assert retry.status_code == 200, retry.text
    revisions = client.get(f"{API}/diaries/{diary_id}/revisions").json()
    assert [item["version"] for item in revisions["versions"]] == [1, 2, 3]
    contents = [item["content"] for item in revisions["versions"]]
    assert contents[0] == "并发验收起点"
    assert "并发写入-0" in contents or "并发写入-1" in contents


# ---------------------------------------------------------------------------
# API 面：必需端点全部存在
# ---------------------------------------------------------------------------
def test_api_surface_contains_all_required_endpoints(client: TestClient) -> None:
    """附加检查：用户要求的端点在 OpenAPI 中全部存在，且不存在无来源写记忆的入口。"""

    spec = client.get("/openapi.json").json()
    available = {
        (method.lower(), path)
        for path, operations in spec["paths"].items()
        for method in operations
        if method in {"get", "post", "put", "patch", "delete"}
    }
    required = {
        ("get", "/health"),
        ("post", f"{API}/diaries"),
        ("get", f"{API}/diaries"),
        ("get", f"{API}/diaries/{{diary_id}}"),
        ("put", f"{API}/diaries/{{diary_id}}"),
        ("delete", f"{API}/diaries/{{diary_id}}"),
        ("get", f"{API}/diaries/{{diary_id}}/revisions"),
        ("get", f"{API}/diaries/{{diary_id}}/revisions/{{version}}"),
        ("post", f"{API}/diaries/{{diary_id}}/restore"),
        ("delete", f"{API}/diaries/{{diary_id}}/permanent"),
        ("post", f"{API}/memories/extract"),
        ("get", f"{API}/memories"),
        ("get", f"{API}/memories/{{memory_id}}"),
        ("get", f"{API}/memories/{{memory_id}}/sources"),
        ("post", f"{API}/memories/{{memory_id}}/confirm"),
        ("post", f"{API}/memories/{{memory_id}}/correct"),
        ("post", f"{API}/memories/{{memory_id}}/revoke"),
        ("get", f"{API}/search"),
        ("post", f"{API}/conversations"),
        ("get", f"{API}/conversations/{{conversation_id}}"),
        ("post", f"{API}/conversations/{{conversation_id}}/messages"),
        ("get", f"{API}/insights/weekly"),
        ("get", f"{API}/insights/monthly"),
        ("get", f"{API}/export"),
    }
    assert required <= available, sorted(required - available)
    assert spec["info"]["title"]


# ---------------------------------------------------------------------------
# 可选 API Token / 管理员权限 / 永久删除
# ---------------------------------------------------------------------------
def test_optional_api_token_and_admin_protection(tmp_path: Path) -> None:
    """可选 Token：API 全量保护（health 公开）；永久删除需 admin + 确认头，未配置时禁用。"""

    settings = _settings(tmp_path, api_token="qa-api-token", admin_token="qa-admin-token")
    app = create_app(settings)
    try:
        with TestClient(app) as client:
            assert client.get(f"{API}/diaries").status_code == 401
            assert client.get(f"{API}/diaries", headers={"Authorization": "Bearer wrong"}).status_code == 401
            assert client.get("/health").status_code == 200
            headers = {"Authorization": "Bearer qa-api-token"}
            created = client.post(f"{API}/diaries", json={"content": "受保护的日记"}, headers=headers)
            assert created.status_code == 201
            diary_id = created.json()["id"]

            # 无 admin token header / 错误值 -> 403
            assert (
                client.delete(
                    f"{API}/diaries/{diary_id}/permanent",
                    headers={**headers, "X-Confirm-Permanent-Delete": "true"},
                ).status_code
                == 403
            )
            # 正确的 admin，但缺少/错误的二次确认 -> 422
            admin_headers = {**headers, "X-Admin-Token": "qa-admin-token"}
            assert (
                client.delete(f"{API}/diaries/{diary_id}/permanent", headers=admin_headers).status_code == 422
            )
            assert (
                client.delete(
                    f"{API}/diaries/{diary_id}/permanent",
                    headers={**admin_headers, "X-Confirm-Permanent-Delete": "nope"},
                ).status_code
                == 422
            )
            # 完整条件满足 -> 物理删除成功，修订随之消失
            deleted = client.delete(
                f"{API}/diaries/{diary_id}/permanent",
                headers={**admin_headers, "X-Confirm-Permanent-Delete": diary_id},
            )
            assert deleted.status_code == 200, deleted.text
            assert deleted.json()["permanent"] is True
            assert client.get(f"{API}/diaries/{diary_id}", headers=headers).status_code == 404
    finally:
        app.state.engine.dispose()

    # 未配置 HEARTLOG_ADMIN_TOKEN 时永久删除整体禁用。
    disabled_settings = _settings(tmp_path / "no-admin", admin_token=None)
    app2 = create_app(disabled_settings)
    try:
        with TestClient(app2) as client:
            diary = client.post(f"{API}/diaries", json={"content": "管理员令牌未配置"}).json()
            response = client.delete(
                f"{API}/diaries/{diary['id']}/permanent",
                headers={"X-Confirm-Permanent-Delete": "true"},
            )
            _assert_error(response, 403, "admin_disabled")
            assert client.get(f"{API}/diaries/{diary['id']}").status_code == 200
    finally:
        app2.state.engine.dispose()


def test_permanent_delete_refuses_referenced_entries(app: Any, client: TestClient) -> None:
    """永久删除必须拒绝被记忆/人物引用的日记，且软删除仍可用、修订仍保留。"""

    created = client.post(f"{API}/diaries", json={"content": "被引用日记：她说周末会来找我。"}).json()
    diary_id = created["id"]
    _install_ai(app, [_candidate("周末约定", evidence="她说周末会来找我")])
    assert client.post(f"{API}/memories/extract", json={"entry_id": diary_id}).status_code == 200

    headers = {"X-Admin-Token": "test-admin-token", "X-Confirm-Permanent-Delete": "true"}
    response = client.delete(f"{API}/diaries/{diary_id}/permanent", headers=headers)
    _assert_error(response, 409, "entry_has_references")
    assert client.get(f"{API}/diaries/{diary_id}").status_code == 200

    assert client.delete(f"{API}/diaries/{diary_id}").status_code == 200
    assert _count(app, Memory) == 1
    with app.state.session_factory() as session:
        revisions = list(session.scalars(select(DiaryRevision).where(DiaryRevision.entry_id == diary_id)))
        assert len(revisions) == 1


# ---------------------------------------------------------------------------
# 审计隐私 + 完整导出
# ---------------------------------------------------------------------------
def test_audit_privacy_and_complete_export(
    app: Any, client: TestClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """附加检查：审计/日志不含日记正文；导出包含完整数据且不含凭据。"""

    caplog.set_level(logging.INFO)
    first_text = "导出验收原文：第一版私密内容，她说周末会来找我。"
    second_text = "导出验收修订：第二版私密内容。"
    evidence = "她说周末会来找我"

    created = client.post(f"{API}/diaries", json={"content": first_text}).json()
    diary_id = created["id"]
    revision_one_id = created["current_revision_id"]
    assert client.put(f"{API}/diaries/{diary_id}", json={"content": second_text}).status_code == 200

    # 占位对话必须在安装 fake AI 之前完成（AI 关闭时走本地占位路径）。
    conversation = client.post(f"{API}/conversations", json={"title": "导出用对话"}).json()
    exchange = client.post(f"{API}/conversations/{conversation['id']}/messages", json={"content": "你好"})
    assert exchange.status_code == 200

    _install_ai(app, [_candidate("周末会见面", evidence=evidence)])
    extracted = client.post(f"{API}/memories/extract", json={"entry_id": diary_id, "version": 1})
    assert extracted.status_code == 200, extracted.text
    memory_id = extracted.json()["created"][0]["id"]
    assert client.post(f"{API}/memories/{memory_id}/confirm").status_code == 200

    person = client.post(
        f"{API}/persons",
        json={
            "display_name": "小夏",
            "aliases": ["夏夏"],
            "source_entry_id": diary_id,
            "source_revision_id": revision_one_id,
            "source_evidence": evidence,
        },
    )
    assert person.status_code == 201, person.text

    deleted = client.post(f"{API}/diaries", json={"content": "导出验收：软删除的日记"}).json()
    assert client.delete(f"{API}/diaries/{deleted['id']}").status_code == 200

    response = client.get(f"{API}/export")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["schema_version"] == "heartlog.export.v1"
    assert payload["counts"] == {
        "diaries": 2,
        "revisions": 3,
        "memories": 1,
        "memory_sources": 1,
        "persons": 1,
        "conversations": 1,
        "audit_events": len(payload["audit_events"]),
    }
    exported_by_id = {item["id"]: item for item in payload["diaries"]}
    first_export = exported_by_id[diary_id]
    assert first_export["original_text"] == first_text
    assert [item["content"] for item in first_export["revisions"]] == [first_text, second_text]
    assert exported_by_id[deleted["id"]]["deleted_at"] is not None
    exported_memory = payload["memories"][0]
    assert exported_memory["verification_status"] == "confirmed"
    assert exported_memory["sources"][0]["revision_id"] == revision_one_id
    assert payload["persons"][0]["display_name"] == "小夏"
    assert len(payload["conversations"][0]["messages"]) == 2
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "test-admin-token" not in serialized
    assert "qa-api-token" not in serialized
    assert "ai_api_key" not in serialized.lower()

    # 审计事件只含结构信息，绝不复制日记/证据正文。
    with app.state.session_factory() as session:
        events = list(session.scalars(select(AuditEvent)))
    details = [dict(event.details) for event in events]
    event_types = [event.event_type for event in events]
    assert details
    audit_blob = json.dumps(details, ensure_ascii=False)
    assert first_text not in audit_blob
    assert second_text not in audit_blob
    assert evidence not in audit_blob
    assert "diary.created" in event_types
    assert "diary.revision_created" in event_types
    assert "memories.extracted" in event_types
    assert "memory.confirmed" in event_types
    assert "data.exported" in event_types

    # 正常路径的服务日志同样不应包含日记正文。
    log_blob = caplog.text
    assert first_text not in log_blob
    assert second_text not in log_blob


# ---------------------------------------------------------------------------
# 跨测试隔离：不触碰真实数据库
# ---------------------------------------------------------------------------
def test_suite_uses_isolated_temporary_database(settings: Any, tmp_path: Path) -> None:
    """附加检查：测试使用的数据库位于 tmp_path，绝不指向项目 data/heartlog.db。"""

    database_path = Path(make_url(settings.database_url).database or "").resolve()
    resolved_tmp = tmp_path.resolve()
    assert resolved_tmp == database_path.parent or resolved_tmp in database_path.parents
    assert settings.environment == "test"
    assert settings.data_dir.resolve().is_relative_to(resolved_tmp)
    assert settings.backup_dir.resolve().is_relative_to(resolved_tmp)
    project_db = (Path(__file__).resolve().parents[1] / "data" / "heartlog.db").resolve()
    assert database_path != project_db


# ---------------------------------------------------------------------------
# 备份 / 恢复脚本可用性（in-process 调用 CLI main，不启动服务）
# ---------------------------------------------------------------------------
def test_backup_and_restore_scripts_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """附加检查：备份脚本生成校验文件/manifest，恢复脚本可还原出完整日记与修订。"""

    source_db = tmp_path / "source.db"
    settings = _settings(
        tmp_path / "source",
        database_url=f"sqlite:///{source_db.as_posix()}",
        data_dir=tmp_path / "source" / "data",
        backup_dir=tmp_path / "source" / "backups",
    )
    app = create_app(settings)
    try:
        with TestClient(app) as client:
            created = client.post(f"{API}/diaries", json={"content": "备份验收原文"}).json()
            diary_id = created["id"]
            assert (
                client.put(f"{API}/diaries/{diary_id}", json={"content": "备份验收修订"}).status_code == 200
            )
    finally:
        app.state.engine.dispose()

    monkeypatch.setenv("HEARTLOG_DATABASE_URL", f"sqlite:///{source_db.as_posix()}")
    monkeypatch.setenv("HEARTLOG_DATA_DIR", str(tmp_path / "source" / "data"))
    monkeypatch.setenv("HEARTLOG_BACKUP_DIR", str(tmp_path / "source" / "backups"))
    monkeypatch.setenv("HEARTLOG_AUTO_CREATE_SCHEMA", "false")
    get_settings.cache_clear()
    backup_file = tmp_path / "backups" / "snapshot.db"
    restored_db = tmp_path / "restored.db"
    try:
        from scripts import backup as backup_script
        from scripts import restore as restore_script

        assert backup_script.main(["--output", str(backup_file)]) == 0
        assert backup_file.exists()
        assert backup_file.with_suffix(backup_file.suffix + ".sha256").exists()
        manifest = json.loads(
            backup_file.with_suffix(backup_file.suffix + ".json").read_text(encoding="utf-8")
        )
        assert manifest["integrity_check"] == "ok"
        assert manifest["sha256"] == backup_script.sha256_file(backup_file)
        assert manifest["table_counts"]["diary_entries"] == 1
        assert manifest["table_counts"]["diary_revisions"] == 2

        monkeypatch.setenv("HEARTLOG_DATABASE_URL", f"sqlite:///{restored_db.as_posix()}")
        get_settings.cache_clear()
        assert restore_script.main([str(backup_file)]) == 0
        assert restored_db.exists()
    finally:
        for key in (
            "HEARTLOG_DATABASE_URL",
            "HEARTLOG_DATA_DIR",
            "HEARTLOG_BACKUP_DIR",
            "HEARTLOG_AUTO_CREATE_SCHEMA",
        ):
            monkeypatch.delenv(key, raising=False)
        get_settings.cache_clear()

    restored_settings = _settings(
        tmp_path / "restored",
        database_url=f"sqlite:///{restored_db.as_posix()}",
        data_dir=tmp_path / "restored" / "data",
        backup_dir=tmp_path / "restored" / "backups",
    )
    restored_app = create_app(restored_settings)
    try:
        with TestClient(restored_app) as client:
            detail = client.get(f"{API}/diaries/{diary_id}")
            assert detail.status_code == 200, detail.text
            assert detail.json()["original_text"] == "备份验收原文"
            assert detail.json()["current_version"] == 2
            revisions = client.get(f"{API}/diaries/{diary_id}/revisions").json()
            assert [item["content"] for item in revisions["versions"]] == [
                "备份验收原文",
                "备份验收修订",
            ]
    finally:
        restored_app.state.engine.dispose()
