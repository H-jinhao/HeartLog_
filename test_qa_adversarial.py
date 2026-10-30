"""独立对抗性 QA 测试（由 qa-dev 编写）。

目标不是重复既有测试，而是从攻击者/边缘用户视角寻找：
* 无效边缘输入导致 5xx、日志或响应泄漏私人内容；
* 并发/事务边界导致的错误语义；
* 绕过不可变/来源校验/审核状态的 API 路径；
* 备份/恢复等运维脚本的失败模式。

文件开头三个测试是 QA 期间发现的真实缺陷（BUG-1/2/3）的**回归测试**：
缺陷在 QA 期间修复，因此这些测试现在断言正确行为并应保持绿色
（先前以 xfail 记录失败行为，修复后已移除 xfail 标记，未弱化任何断言）。
其余测试全部应为绿色。所有测试离线、使用 conftest 的临时 SQLite，
不触碰 data/heartlog.db。
"""

from __future__ import annotations

import hashlib
import json
import logging
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.core.exceptions import ConflictError
from app.core.exceptions import ValidationError as AppValidationError
from app.models.diary import DiaryEntry, DiaryRevision
from app.models.memory import Memory, MemorySource
from app.schemas.ai import CandidateMemory, ExtractionEnvelope
from app.services.diary_service import DiaryService

API = "/api/v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class DictFakeAI:
    """Fake AI whose extract_memories returns a raw dict (bypasses service schema)."""

    configured = True
    provider_name = "qa-dict-fake"
    model_name = "qa-dict-model"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def extract_memories(self, **_kwargs: Any) -> dict[str, Any]:
        return self.payload


class EnvelopeFakeAI:
    """Fake AI returning a valid envelope with deterministic candidates."""

    configured = True
    provider_name = "qa-env-fake"
    model_name = "qa-env-model"

    def __init__(self, candidates: list[CandidateMemory]) -> None:
        self.candidates = candidates

    async def extract_memories(self, **_kwargs: Any) -> ExtractionEnvelope:
        return ExtractionEnvelope(
            candidates=list(self.candidates),
            provider=self.provider_name,
            model=self.model_name,
        )


class LogCollector(logging.Handler):
    """Capture messages *and* formatted exception tracebacks (unlike caplog.text)."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())
        if record.exc_info:
            self.records.append("".join(traceback.format_exception(*record.exc_info)))

    @property
    def blob(self) -> str:
        return "\n".join(self.records)


@pytest.fixture()
def log_collector() -> Any:
    collector = LogCollector()
    logger = logging.getLogger("heartlog")
    previous_level = logger.level
    # 说明：tests/test_migrations.py 通过 alembic 的 fileConfig(disable_existing_loggers=True)
    # 会永久禁用已存在的 "heartlog" logger（跨测试污染）。这里显式恢复启用状态，
    # 使日志断言与测试执行顺序无关；测试结束后恢复原状态。
    previous_disabled = logger.disabled
    logger.disabled = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(collector)
    try:
        yield collector
    finally:
        logger.removeHandler(collector)
        logger.setLevel(previous_level)
        logger.disabled = previous_disabled


def _count(app: Any, model: type[Any]) -> int:
    with app.state.session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _candidate(content: str, *, evidence: str) -> CandidateMemory:
    return CandidateMemory(content=content, memory_type="explicit_fact", evidence_text=evidence)


# ---------------------------------------------------------------------------
# 回归 1（QA 发现 BUG-1，已修复）：NUL/控制字符必须返回 422，不得 500
# ---------------------------------------------------------------------------
def test_nul_and_control_characters_are_rejected_as_422(client: TestClient, app: Any) -> None:
    """需求 10：NUL/C0 控制字符输入必须得到领域 4xx，且错误响应不回显内容。"""

    secret = "NUL回归-绝密内容-MARKER-QA-042"
    for content in (
        "\u0000",
        "\u0000" + "x" * 10,
        "\u0000" + secret,
        "正常\x01控制字符",
        "正常\x7fDEL",
    ):
        response = client.post(f"{API}/diaries", json={"content": content})
        assert response.status_code == 422, (
            f"forbidden control char content returned {response.status_code}: {response.text}"
        )
        assert response.json()["error"]["code"] == "validation_error"
        assert secret not in response.text

    # 合法控制字符（制表/换行/回车）不能被一刀切拒绝，且逐字符保存。
    allowed = "第一行\t制表\n第二行\r\n第三行"
    created = client.post(f"{API}/diaries", json={"content": allowed})
    assert created.status_code == 201, created.text
    assert created.json()["original_text"] == allowed

    # 编辑接口同样必须拒绝 NUL。
    put_response = client.put(f"{API}/diaries/{created.json()['id']}", json={"content": "修订\u0000内容"})
    assert put_response.status_code == 422

    # 服务层纵深防御：绕过 HTTP schema 直接调用服务也必须拒绝（invalid_content）。
    with app.state.session_factory() as session:
        service = DiaryService(session, app.state.settings)
        with pytest.raises(AppValidationError) as excinfo:
            service.create(content="\u0000服务层绕过")
        assert excinfo.value.code == "invalid_content"
        session.rollback()
    assert client.get(f"{API}/diaries").json()["total"] == 1


# ---------------------------------------------------------------------------
# 回归 2（QA 发现 BUG-2，已修复）：数据库错误日志不得包含 SQL/日记正文
# ---------------------------------------------------------------------------
def test_database_error_handler_never_logs_sql_or_diary_text(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, log_collector: Any
) -> None:
    """附加检查：SQLAlchemyError 处理器只记录异常类型，不记录 SQL/绑定参数/正文。"""

    secret = "数据库错误日志回归-绝密日记内容-MARKER-QA-043"

    def exploding_commit(self: Any) -> None:
        # 模拟真实的 SQLAlchemy 数据库错误：异常文本包含 SQL 语句与绑定参数。
        raise OperationalError(
            "INSERT INTO diary_entries (original_text) VALUES (?)",
            {"original_text": secret},
            Exception("simulated database failure"),
        )

    monkeypatch.setattr(DiaryService, "_commit", exploding_commit)
    response = client.post(f"{API}/diaries", json={"content": secret})
    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "database_error"
    assert secret not in response.text
    assert "INSERT INTO" not in response.text
    # 事务确实回滚：失败的创建没有留下半条日记。
    assert client.get(f"{API}/diaries").json()["total"] == 0

    blob = log_collector.blob
    # 防止"日志为空所以不含正文"的假阳性：必须真的捕获到该数据库错误日志。
    assert "database_error" in blob
    assert "OperationalError" in blob
    assert "Traceback" not in blob
    assert secret not in blob
    assert "INSERT INTO" not in blob


# ---------------------------------------------------------------------------
# 回归 3（QA 发现 BUG-3，已修复）：陈旧读快照并发写入 -> 409 领域冲突
# ---------------------------------------------------------------------------
def test_stale_session_edit_returns_version_conflict(client: TestClient, app: Any) -> None:
    """需求 20：陈旧读快照上的并发写入必须映射为 409 version_conflict，而非 500。"""

    created = client.post(f"{API}/diaries", json={"content": "陈旧会话并发终点"}).json()
    diary_id = created["id"]
    factory = app.state.session_factory
    stale_session = factory()
    writer_session = factory()
    try:
        stale_service = DiaryService(stale_session, app.state.settings)
        stale_service.get(diary_id)  # 读 version=1 并开启读事务
        writer_service = DiaryService(writer_session, app.state.settings)
        assert (
            writer_service.update(
                diary_id, content="会话B已提交", expected_version=1, change_reason="B"
            ).current_version
            == 2
        )

        with pytest.raises(ConflictError) as excinfo:
            stale_service.update(
                diary_id,
                content="会话A基于旧快照写入",
                expected_version=None,
                change_reason="A",
            )
        assert excinfo.value.code == "version_conflict"
        assert excinfo.value.status_code == 409
    finally:
        stale_session.rollback()
        writer_session.rollback()
        stale_session.close()
        writer_session.close()

    with app.state.session_factory() as session:
        entry = session.get(DiaryEntry, diary_id)
        assert entry is not None and entry.current_version == 2
        revisions = list(session.scalars(select(DiaryRevision).where(DiaryRevision.entry_id == diary_id)))
        assert [revision.version for revision in revisions] == [1, 2], "冲突不得静默写入任何修订"


# ---------------------------------------------------------------------------
# 隐私（正常路径）：请求/业务日志绝不包含日记正文
# ---------------------------------------------------------------------------
def test_normal_operation_logs_never_contain_diary_text(
    client: TestClient, app: Any, log_collector: Any
) -> None:
    secret_one = "正常日志隐私验收-私密原文-MARKER-ONE"
    secret_two = "正常日志隐私验收-私密修订-MARKER-TWO"

    created = client.post(f"{API}/diaries", json={"content": secret_one}).json()
    diary_id = created["id"]
    revision_one_id = created["current_revision_id"]
    assert client.put(f"{API}/diaries/{diary_id}", json={"content": secret_two}).status_code == 200
    assert client.get(f"{API}/search", params={"q": "MARKER-ONE"}).status_code == 200
    app.state.ai_service = EnvelopeFakeAI([_candidate("隐私记忆", evidence="MARKER-ONE")])
    assert (
        client.post(
            f"{API}/memories/extract",
            json={"entry_id": diary_id, "revision_id": revision_one_id},
        ).status_code
        == 200
    )
    assert client.get(f"{API}/export").status_code == 200
    assert client.delete(f"{API}/diaries/{diary_id}").status_code == 200

    blob = log_collector.blob
    assert secret_one not in blob
    assert secret_two not in blob
    assert "MARKER-ONE" not in blob
    assert "MARKER-TWO" not in blob


# ---------------------------------------------------------------------------
# 伪造来源：模型输出不能自带来源身份；跨日记证据被拒
# ---------------------------------------------------------------------------
def test_fabricated_model_source_identity_is_rejected(client: TestClient, app: Any) -> None:
    diary = client.post(f"{API}/diaries", json={"content": "伪造来源对抗测试原文"}).json()
    payload = {
        "candidates": [
            {
                "content": "模型声称的来源",
                "memory_type": "explicit_fact",
                "evidence_text": "伪造",
                # 模型试图自带 source_entry_id / source_revision_id：schema 必须拒绝。
                "source_entry_id": "attacker-controlled-entry",
                "source_revision_id": "attacker-controlled-revision",
            }
        ],
        "provider": "attacker",
        "model": "attacker",
        "mode": "ai",
    }
    app.state.ai_service = DictFakeAI(payload)
    response = client.post(f"{API}/memories/extract", json={"entry_id": diary["id"]})
    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "ai_invalid_response"
    assert "attacker-controlled-entry" not in response.text
    assert _count(app, Memory) == 0
    assert _count(app, MemorySource) == 0


def test_model_evidence_from_another_diary_is_rejected(client: TestClient, app: Any) -> None:
    first = client.post(f"{API}/diaries", json={"content": "第一篇日记：她说周末会来。"}).json()
    second = client.post(f"{API}/diaries", json={"content": "第二篇日记：她说周末不会来。"}).json()
    stolen_evidence = "她说周末不会来"
    app.state.ai_service = EnvelopeFakeAI([_candidate("从另一篇日记偷来的断言", evidence=stolen_evidence)])
    response = client.post(f"{API}/memories/extract", json={"entry_id": first["id"]})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "no_valid_candidates"
    assert _count(app, Memory) == 0
    # 目标日记确实存在，只是证据不属于它。
    assert client.get(f"{API}/diaries/{second['id']}").status_code == 200


# ---------------------------------------------------------------------------
# 无来源记忆：不存在任何 API 写入入口
# ---------------------------------------------------------------------------
def test_no_api_path_can_create_a_sourceless_memory(client: TestClient) -> None:
    assert client.get(f"{API}/memories").status_code == 200
    for method in ("post", "put", "patch"):
        response = getattr(client, method)(f"{API}/memories", json={"content": "无来源"})
        assert response.status_code == 405, (method, response.text)
    assert client.delete(f"{API}/memories").status_code == 405
    # GET /memories/extract 会被 /memories/{memory_id} 匹配 -> 404；都不是写入路径。
    assert client.get(f"{API}/memories/extract").status_code in {404, 405}
    assert client.get(f"{API}/memories/some-id/confirm").status_code == 405


# ---------------------------------------------------------------------------
# 不可变字段不可通过 API 批量赋值绕过
# ---------------------------------------------------------------------------
def test_api_cannot_mass_assign_immutable_diary_fields(client: TestClient) -> None:
    original = "不可变字段对抗：原始文本"
    created = client.post(
        f"{API}/diaries",
        json={
            "content": original,
            "original_text": "攻击者覆盖原文",
            "original_content_hash": "0" * 64,
            "current_version": 99,
            "id": "attacker-id",
        },
    )
    assert created.status_code == 422, created.text
    assert client.get(f"{API}/diaries").json()["total"] == 0

    diary = client.post(f"{API}/diaries", json={"content": original}).json()
    overwrite = client.put(
        f"{API}/diaries/{diary['id']}",
        json={"content": "合法修订", "original_text": "攻击者覆盖原文"},
    )
    assert overwrite.status_code == 422, overwrite.text
    detail = client.get(f"{API}/diaries/{diary['id']}").json()
    assert detail["original_text"] == original
    assert detail["current_version"] == 1


# ---------------------------------------------------------------------------
# 恶意/畸形 ID 与 SQL 注入不影响系统
# ---------------------------------------------------------------------------
def test_pathological_ids_and_sql_injection_are_handled(client: TestClient) -> None:
    dangerous_ids = [
        "does-not-exist",
        "a" * 500,
        "' OR 1=1 --",
        "'; DROP TABLE diary_entries; --",
        "../../../etc/passwd",
        "00000000-0000-0000-0000-000000000000",
        "中文不存在的-ID",
        "100%",
    ]
    for identifier in dangerous_ids:
        encoded = quote(identifier, safe="")
        for path in (
            f"{API}/diaries/{encoded}",
            f"{API}/diaries/{encoded}/revisions",
            f"{API}/diaries/{encoded}/revisions/1",
            f"{API}/diaries/{encoded}/integrity",
            f"{API}/memories/{encoded}",
            f"{API}/memories/{encoded}/sources",
        ):
            response = client.get(path)
            assert response.status_code in {400, 404, 422}, (path, response.status_code, response.text)
            assert response.status_code < 500

    created = client.post(f"{API}/diaries", json={"content": "SQL 注入防护验收"}).json()
    for query in ("'; DROP TABLE diary_entries; --", "%", "_", "' OR '1'='1"):
        response = client.get(f"{API}/search", params={"q": query})
        assert response.status_code == 200, response.text
    assert client.get(f"{API}/diaries/{created['id']}").status_code == 200
    assert client.get(f"{API}/diaries").json()["total"] == 1


# ---------------------------------------------------------------------------
# 特殊正文逐字符往返（emoji/CRLF/组合字符/长文本）
# ---------------------------------------------------------------------------
def test_special_content_roundtrips_exactly(client: TestClient) -> None:
    content = "Emoji 😀👨‍👩‍👧 组合\n\r\t第二行\u200b零宽\u202eRTL\u0301combining\n" + "长文本-" * 1000
    created = client.post(f"{API}/diaries", json={"content": content})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["original_text"] == content
    assert body["current_content"] == content
    assert body["original_content_hash"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert client.get(f"{API}/diaries/{body['id']}").json()["original_text"] == content
    revisions = client.get(f"{API}/diaries/{body['id']}/revisions").json()
    assert revisions["versions"][0]["content"] == content

    found = client.get(f"{API}/search", params={"q": "零宽", "scope": "diaries"}).json()
    assert found["total"] == 1

    edited = "编辑后的特殊内容 😀 \n\r\t \u200b"
    assert client.put(f"{API}/diaries/{body['id']}", json={"content": edited}).status_code == 200
    detail = client.get(f"{API}/diaries/{body['id']}").json()
    assert detail["original_text"] == content
    assert detail["current_content"] == edited


# ---------------------------------------------------------------------------
# 软删除后的编辑必须 404，且不得静默追加修订
# ---------------------------------------------------------------------------
def test_editing_soft_deleted_diary_is_rejected_without_writes(client: TestClient, app: Any) -> None:
    created = client.post(f"{API}/diaries", json={"content": "软删除后仍尝试编辑"}).json()
    diary_id = created["id"]
    assert client.delete(f"{API}/diaries/{diary_id}").status_code == 200
    response = client.put(f"{API}/diaries/{diary_id}", json={"content": "不应写入的修订"})
    assert response.status_code == 404, response.text
    with app.state.session_factory() as session:
        revisions = list(session.scalars(select(DiaryRevision).where(DiaryRevision.entry_id == diary_id)))
        assert len(revisions) == 1
        assert revisions[0].content == "软删除后仍尝试编辑"


# ---------------------------------------------------------------------------
# 其它校验边界
# ---------------------------------------------------------------------------
def test_misc_validation_edges_return_4xx(client: TestClient) -> None:
    created = client.post(f"{API}/diaries", json={"content": "边界校验日记"}).json()
    diary_id = created["id"]

    reversed_range = client.get(
        f"{API}/diaries",
        params={"date_from": "2026-01-02T00:00:00Z", "date_to": "2026-01-01T00:00:00Z"},
    )
    assert reversed_range.status_code == 422
    assert reversed_range.json()["error"]["code"] == "validation_error"

    long_title = client.post(f"{API}/conversations", json={"title": "标" * 201})
    assert long_title.status_code == 422
    conversation = client.post(f"{API}/conversations", json={"title": "边界对话"}).json()
    blank_message = client.post(f"{API}/conversations/{conversation['id']}/messages", json={"content": "   "})
    assert blank_message.status_code == 422
    assert client.get(f"{API}/conversations/unknown").status_code == 404

    # 记忆审核类输入的边界同样必须是 4xx。
    assert client.get(f"{API}/memories", params={"active": "not-a-bool"}).status_code == 422
    assert client.get(f"{API}/memories", params={"limit": 0}).status_code == 422
    assert client.get(f"{API}/memories", params={"offset": -1}).status_code == 422
    assert client.post(f"{API}/memories/{diary_id}/correct", json={"content": ""}).status_code in {
        404,
        422,
    }


# ---------------------------------------------------------------------------
# 备份/恢复脚本的失败模式：篡改校验和必须拒绝恢复
# ---------------------------------------------------------------------------
def test_backup_manifest_and_tampered_restore_are_rejected(
    client: TestClient, settings: Any, tmp_path: Path
) -> None:
    from scripts.backup import backup_database, sha256_file
    from scripts.restore import verify_backup

    client.post(f"{API}/diaries", json={"content": "备份脚本对抗测试"})
    database_path = Path(make_url(settings.database_url).database or "")
    backup_path = tmp_path / "tamper-backup.db"
    manifest = backup_database(database_path, backup_path)
    assert manifest["integrity_check"] == "ok"
    assert manifest["sha256"] == sha256_file(backup_path)
    checksum_path = backup_path.with_suffix(backup_path.suffix + ".sha256")
    assert checksum_path.exists()
    manifest_path = backup_path.with_suffix(backup_path.suffix + ".json")
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["sha256"] == manifest["sha256"]

    # 备份脚本拒绝覆盖已有目标（防止误覆盖历史备份）。
    with pytest.raises(SystemExit):
        backup_database(database_path, backup_path)

    # 篡改 .sha256 后恢复脚本必须拒绝。
    checksum_path.write_text("0" * 64 + "\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        verify_backup(backup_path)

    # 还原真实校验和后可通过校验。
    checksum_path.write_text(manifest["sha256"] + "\n", encoding="utf-8")
    verify_backup(backup_path)
