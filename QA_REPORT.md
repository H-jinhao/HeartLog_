# HeartLog 独立 QA 报告

- 项目：HeartLog（local-first 情绪日记 + 可溯源长期记忆，FastAPI + SQLite）
- 工作目录：`D:\Harness\heartlog`
- QA 执行者：qa-dev（独立于实现者）
- 执行日期：2026-10-29
- 版本：`0.1.0`
- 本报告只记录实际执行过的命令与真实结果；未验证的项目在第 7 节明确列出。

---

## 1. 环境与执行命令

### 1.1 环境

| 项目 | 值 |
| --- | --- |
| 解释器 | `C:/Users/H/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe`（Python 3.12.14） |
| PYTHONPATH | `D:\Harness\heartlog\.pyshim;D:\Harness\heartlog\.deps;D:\Harness\heartlog`（`.pyshim` 为本机 Windows ACL 必需 shim，全部命令均携带） |
| 测试框架 | pytest 9.2.0（`.deps` 内固定版本），`pyproject.toml` 已配置 `testpaths=["tests"]`、`addopts="-ra"` |
| 数据隔离 | 每个测试使用 `tests/conftest.py` 的 `tmp_path` 临时 SQLite；**未打开、未修改**真实 `data/heartlog.db`（该文件在运行前后均不存在，`data/` 仅有 `.gitkeep`；`backups/` 为空） |
| AI / 网络 | 全部离线：AI 默认禁用（`ai_enabled=false, ai_provider=disabled`）；记忆提取使用进程内 fake；另外用 `monkeypatch` 将 `httpx.AsyncClient` 替换为"一旦实例化就抛错"的炸弹以证明禁用路径不建立网络客户端 |
| 外部服务 | 未调用任何外部 AI / 网络服务；未启动常驻 HTTP 服务（全部 TestClient / 临时引擎） |

### 1.2 实际执行的命令（均在 `D:\Harness\heartlog`）

```bash
PY='C:/Users/H/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe'
export PYTHONPATH='D:\Harness\heartlog\.pyshim;D:\Harness\heartlog\.deps;D:\Harness\heartlog'

# 1) QA 开始时的基线（Lead/队友实现完成后、QA 新增测试前）
"$PY" -m pytest tests -q
# => 137 passed, 1 warning in 17.67s

# 2) 新增验收 / 对抗测试的独立运行
"$PY" -m pytest tests/test_acceptance.py -q      # => 20 passed
"$PY" -m pytest tests/test_qa_adversarial.py -q  # => 13 passed（修复前为 10 passed, 3 xfailed）

# 3) 最终全量套件（本次交付的权威数字，以收尾前最后一次执行为准）
"$PY" -m pytest tests -q
# => 172 passed, 1 warning in 22.56s（退出码 0；重复执行耗时在 22.5–23.5s 之间波动）

# 4) 新增测试的静态检查
"$PY" -m ruff check tests/test_acceptance.py tests/test_qa_adversarial.py      # => All checks passed!
"$PY" -m ruff format --check tests/test_acceptance.py tests/test_qa_adversarial.py  # => 2 files already formatted
```

未安装任何依赖（未执行 pip install）。未修改应用源码、迁移、prompts、配置、文档或既有测试。

---

## 2. 最终测试结果（精确数字）

```
$ ... -m pytest tests -q
........................................................................ [ 41%]
........................................................................ [ 83%]
............................                                             [100%]
============================== warnings summary ===============================
... StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
172 passed, 1 warning in 22.56s
```

（退出码 0；`pytest tests -q` 在收尾前多次执行均为 `172 passed`，耗时 22.5–23.5s。）

| 指标 | 数值 |
| --- | --- |
| 收集用例总数 | **172** |
| 通过 | **172** |
| 失败 | **0** |
| 跳过 / xfail / xpass | **0 / 0 / 0**（BUG 修复后已移除全部 xfail 标记，改为断言正确行为的回归测试） |
| 警告 | **1**（Starlette/httpx TestClient 弃用警告，既有问题，与产品行为无关） |
| 耗时 | 22.56s（多次重复运行 22.5–23.5s；新增测试合计约 12s，整体仍为快速、确定性、离线） |

数量拆解：

| 来源 | 用例数 | 结果 |
| --- | --- | --- |
| QA 开始时既有套件 | 137（基线 `137 passed`） | 通过 |
| Lead 在 QA 期间补充的回归测试（`tests/test_health.py`：隐私响应头 + 空白 token 视为未配置） | +2（既有套件现为 139） | 通过 |
| QA 新增 `tests/test_acceptance.py` | 20 | 全部通过（4.69s） |
| QA 新增 `tests/test_qa_adversarial.py` | 13 | 全部通过（2.67s，含 3 个缺陷回归测试） |
| **合计** | **172** | **172 passed** |

说明：QA 新增的 3 个对抗测试最初以 `xfail` 记录当时未修复的真实缺陷（BUG-1/2/3）；Lead 修复后，QA **删除 xfail 标记并把断言收紧为正确行为**（未放宽或删除任何断言），现为绿色回归测试。

---

## 3. QA 期间发现、由 Lead 修复的缺陷（found during QA, fixed by Lead）

这三个缺陷均由 qa-dev 在独立对抗测试中发现并给出最小复现，Lead 在 QA 期间修复；修复后 qa-dev 重新验证并保留为回归测试。修复前/后的实际观察均已确认。

### BUG-1（需求 10，真实缺陷，已修复）：以 NUL 开头的日记正文导致 HTTP 500

- 触发：`POST /api/v1/diaries`，`{"content": "\u0000"}`（或 `"\u0000任意私密内容"`）。
- 根因：Pydantic 只校验非空白/长度，放行了 NUL；SQLite 的 `length()` 在首个 NUL 处截断为 0，触发 `CHECK(length(original_text) > 0)` 失败 → `IntegrityError` → 500 `database_error`。用户输入导致了服务端 500，而不是 4xx。
- 修复前：`422` 期望失败，实际 `500 {"error":{"code":"database_error",...}}`。
- 修复后（QA 复验）：

  ```text
  BUG-1 NUL status: 422 body: {"error":{"code":"validation_error","message":"Request validation failed",
  "details":{"fields":[{"loc":"body.content","msg":"Value error, content must not contain NUL or
  unsupported control characters","type":"value_error"}]}}}
  ```

- 修复内容：新增 `app/core/text.py` 的 `has_forbidden_control_chars()`（NUL 及其余 C0 控制字符、DEL 拒绝；`\t \n \r` 保留），在 `DiaryCreate`/`DiaryUpdate` schema 与 `DiaryService._validate_content`（纵深防御，code=`invalid_content`）双重校验。
- 回归测试：`tests/test_qa_adversarial.py::test_nul_and_control_characters_are_rejected_as_422` 与 `tests/test_acceptance.py::test_requirement_10_invalid_input_returns_4xx_without_echo`（新增 NUL/控制字符样例）。同时验证合法 `\t \n \r` 不被误拒且逐字符保存。

### BUG-2（日志隐私，真实缺陷，已修复）：数据库错误日志泄漏 SQL 绑定参数（=完整日记正文）

- 触发：任何经过 `app/main.py` `SQLAlchemyError` 处理器的数据库错误（修复前最确定的复现即 BUG-1 的 NUL 输入）。
- 根因：处理器调用 `logger.exception("database_error")`，异常堆栈文本中包含 SQLAlchemy 拼接的 SQL 语句与绑定参数，即完整日记正文及其 SHA-256 哈希；这与 `docs/privacy.md §3` "服务日志不记录私人内容"的承诺矛盾。
- 修复前（qa-dev 抓取日志复现）：日志 traceback 中出现 `[parameters: ('\u0000绝密日记-MARKER...', ...)]`。
- 修复后（QA 复验，注入含正文的 `OperationalError`）：

  ```text
  BUG-2 induced db error status: 500 body: {"error":{"code":"database_error","message":"A database error occurred","details":{}}}
  BUG-2 log lines: ['database_error type=OperationalError']
  BUG-2 secret in logs: False
  BUG-2 SQL in logs: False
  ```

- 修复内容：`app/main.py` 的 SQLAlchemyError 处理器改为只记录异常类名（`logger.error("database_error type=%s", type(exc).__name__)`），不记录 `exc_info`/异常文本；API 响应仍为通用 500，不泄漏 SQL。
- 回归测试：`tests/test_qa_adversarial.py::test_database_error_handler_never_logs_sql_or_diary_text`。该测试通过 monkeypatch `DiaryService._commit` 抛出一个消息中包含 SQL + 私密正文的真实 `sqlalchemy.exc.OperationalError`（确定性、隔离于临时库），并断言：日志确实捕获到该错误（防止"空日志导致假通过"）、不含正文/哈希/SQL/`Traceback`、同时事务回滚（没有留下半条日记）。另有一条正常操作日志隐私测试 `test_normal_operation_logs_never_contain_diary_text`。

### BUG-3（需求 20，真实缺陷，已修复）：陈旧读快照上的并发编辑返回 500，而非 409

- 触发：会话 A 读取某日记（开启 SQLite 读事务）后，会话 B 提交新修订；会话 A 再调用 `DiaryService.update(..., expected_version=None)`，SQLite 返回 `OperationalError: database is locked`（SQLITE_BUSY snapshot）。`update()` 只把 `IntegrityError` 映射为 `ConflictError`，导致原始 `OperationalError` 逃逸；HTTP 层表现为 500 `database_error`（并触发 BUG-2 的日志泄漏路径）。
- 修复前：确定性复现得到 `sqlalchemy.exc.OperationalError ('database is locked')`；HTTP 并发写入（不带 `expected_version`）也可能得到 500。
- 修复后（QA 复验）：同一场景抛出 `ConflictError`（`status_code=409`，`code=version_conflict`），且不写入任何修订。
- 修复内容：`app/services/diary_service.py::update` 增加 `OperationalError` 分支，仅当消息包含 `locked`/`busy` 时映射为 409 `version_conflict`，其他数据库错误继续按 500 处理。
- 回归测试：`tests/test_qa_adversarial.py::test_stale_session_edit_returns_version_conflict`；另有 `test_requirement_20_concurrent_edits_do_not_silently_lose_revisions`（真实两线程并发 HTTP `PUT`：一个 200、一个 409；重试后修订 [1,2,3] 完整，无静默丢失）。

> 备注：BUG-1/2/3 均先写入 QA 的 `xfail` 文档化测试（附精确复现），Lead 修复后 QA 重新验证、删除 xfail 并收紧断言。没有为了变绿而弱化任何断言。

---

## 4. 用户 20 项必需行为 → 测试证据映射

测试位置说明：`A` = `tests/test_acceptance.py`（QA 新增，端到端）；`Q` = `tests/test_qa_adversarial.py`（QA 新增，对抗/回归）；`X` = 既有测试套件（交叉印证）。全部测试均通过。

| # | 用户必需行为 | QA 测试证据（主要） | 结果 |
| --- | --- | --- | --- |
| 1 | 健康检查 | `A::test_requirement_01_health_check`（`/health` 200、database=ok、ai_enabled=false、ai_ready=false；`/` 可用）；`X::test_health.py`（含隐私响应头、token 场景） | 通过 |
| 2 | 创建日记 | `A::test_requirement_02_03_04_create_keeps_exact_original_and_initial_revision`（201、返回 id/current_revision） | 通过 |
| 3 | 原文精确保存 | 同上（含首尾空格、`\n\n`、`\t`、`\r\n`、emoji；`original_text` 逐字符相等；SHA-256 独立计算比对）；`Q::test_special_content_roundtrips_exactly`（emoji/组合字符/零宽/RTL/长文本）；`X::test_diaries.py` | 通过 |
| 4 | 初始修订 | 同上（version=1、revision content 相同、revisions 列表 total=1、integrity 报告有效） | 通过 |
| 5 | 编辑创建新修订 | `A::test_requirement_05_06_07_...`（PUT → version=2/3、change_reason、`previous_revision_hash` 链） | 通过 |
| 6 | 编辑从不覆盖原文 | 同上（断言 `original_text`、v1 content、DB 行原文字段均不变）；`Q::test_api_cannot_mass_assign_immutable_diary_fields`（API 批量赋值 `original_text` → 422 且原文不变） | 通过 |
| 7 | 历史版本可读 | 同上（`GET /revisions` 全部历史 + `GET /revisions/{version}` 逐版本内容 + integrity 哈希链 valid） | 通过 |
| 8 | 重启后数据仍在 | `A::test_requirement_08_data_persists_across_app_restart`（同一 SQLite 文件 `dispose()` 后新建 app：日记两版、历史修订、已确认记忆、检索均仍在） | 通过 |
| 9 | 缺失日记 → 404 | `A::test_requirement_09_missing_resources_return_404`（日记/修订/integrity/记忆/来源/审核端点；错误码 not_found，无堆栈） | 通过 |
| 10 | 非法输入 → 合适 4xx | `A::test_requirement_10_invalid_input_returns_4xx_without_echo`（空/空白/缺字段/多余字段/超长/超 4KiB metadata/非法 expected_version/非法分页与检索参数/NUL/控制字符；均 4xx 且不回显内容）；`Q::test_misc_validation_edges_return_4xx`；**BUG-1 回归** | 通过（修复后） |
| 11 | 软删除默认隐藏 | `A::test_requirement_11_12_soft_delete_hides_entry_but_keeps_revisions`（列表 total=0、详情/修订 404、搜索 diary scope=0、重复删除 404、restore 可恢复；恢复后再 restore → 409） | 通过 |
| 12 | 软删除不删修订 | 同上（直接查 DB：entry 保留 `deleted_at`，2 条修订内容完整；memory 引用亦保留） | 通过 |
| 13 | 无来源记忆不能进入正式库 | `A::test_requirement_13_no_sourceless_memory_can_enter_formal_store`（无创建 API：POST/PUT/DELETE `/memories` → 405；DB 无主来源插入 → IntegrityError）；`A::test_requirement_13_14_15_18_...`（AI 关闭 extract=503 且零写入）；`Q::test_no_api_path_can_create_a_sourceless_memory` | 通过 |
| 14 | 模型推断不能自动成为确认事实 | `A::test_requirement_13_14_15_18_...`（confidence=0.99、`model_inference` 提取后仍 pending、created_by=model；只有显式 `POST /confirm` 才 confirmed，且 `memory_type` 保持 `model_inference`、confirmed_by=user） | 通过 |
| 15 | 记忆指向正确日记 + 修订 | 同上（对 v1 提取：`source_entry_id`、`source_revision_id`=v1、`source_version=1`；`/sources` 返回 revision_version=1 且 offset 切片等于证据文本）；`A::test_requirement_08...`（重启后仍指向 v1 修订） | 通过 |
| 16 | 撤回记忆不出现在正常搜索/列表 | `A::test_requirement_16_revoked_memory_hidden_from_normal_list_and_search`（撤回后默认 list=0、memories scope 搜索=0、`GET /memories/{id}` 仍可追溯、`?verification_status=revoked` 可枚举、重复撤回/确认 → 409） | 通过 |
| 17 | 矛盾来源不自动合并 | `A::test_requirement_17_contradictory_sources_are_not_merged`（两条矛盾日记 → 两条独立记忆，entry_id/内容均不同，搜索 total=2） | 通过 |
| 18 | 模型输出伪造来源被拒 | `A::test_requirement_13_14_15_18_...`（evidence 不在修订中 → `evidence_not_found`；offset 不匹配 → `evidence_offset_mismatch`；全伪造 → 422 `no_valid_candidates` 且 0 写入）；`Q::test_fabricated_model_source_identity_is_rejected`（候选自带 `source_entry_id` → 502 `ai_invalid_response`、0 写入、不回声攻击值）；`Q::test_model_evidence_from_another_diary_is_rejected`（跨日记证据 → 422、0 写入）；`X::test_memories.py`、`X::test_ai_service.py` | 通过 |
| 19 | 关键写入失败回滚事务 | `A::test_requirement_19_failed_critical_write_rolls_back_entire_batch`（第二条候选构建时注入 SQLAlchemyError → 500；memory/memory_source 0 行、无 memory.created/memories.extracted 审计、diary.created 仍在）；`X::test_memories.py`（审计写失败、SAVEPOINT 隔离）、`X::test_revisions.py`（修订写入失败回滚） | 通过 |
| 20 | 并发编辑不静默丢失修订 | `A::test_requirement_20_concurrent_edits_do_not_silently_lose_revisions`（两线程同 `expected_version=1`：恰好一个 200、一个 409 `version_conflict`；修订 [1,2]；冲突方以 v2 重试 → [1,2,3] 全在）；`Q::test_stale_session_edit_returns_version_conflict`（**BUG-3 回归**） | 通过（修复后） |

---

## 5. 附加要求/检查

| 检查项 | 证据 | 结果 |
| --- | --- | --- |
| API 面与用户清单一致 | `A::test_api_surface_contains_all_required_endpoints`：从 `/openapi.json` 断言 24 组必需 method+path 均存在；运行时 OpenAPI 共 25 个 path，与 [docs/api.md](docs/api.md) 端点总览（25 个 path）一致；不存在 `POST /memories` 之类无来源写入入口（405） | 通过 |
| AI 关闭时诚实且有界（503/占位/本地统计），不联网 | `A::test_ai_disabled_is_honest_and_never_uses_network`：monkeypatch `httpx.AsyncClient` 为炸弹；`POST /memories/extract` → 503 `ai_not_configured` 且 Memory/MemorySource/AuditEvent 计数完全不变；对话 → 200 `mode=placeholder`、`ai_used=false`、空 citations、明确本机占位文案、消息已本地保存；weekly/monthly → 200 `mode=local_statistics`、`ai_used=false`、`narrative=null`、notes/disclaimer 存在；`app.state.ai_service.provider is None` | 通过 |
| 可选 API Token / Admin Token 保护 | `A::test_optional_api_token_and_admin_protection`：未带/错误 Bearer → 401；正确 → 200；`/health` 公开；永久删除：缺 admin header → 403、缺/错误确认头 → 422、正确组合 → 200 且 404；未配置 admin token → 403 `admin_disabled`；`X::test_health.py` 补充空白 token 视为未配置 | 通过 |
| 永久删除需 admin + 明确确认 + 拒绝被引用记录 | `A::test_permanent_delete_refuses_referenced_entries`：被记忆引用的日记 → 409 `entry_has_references`（entry 仍在）；软删除仍可用、修订/记忆保留；`A::test_optional_api_token_and_admin_protection` 覆盖确认头/令牌分支 | 通过 |
| 审计日志不包含完整日记正文 | `A::test_audit_privacy_and_complete_export`：全部 AuditEvent.details 序列化后不含原文/修订/证据文本；事件类型齐全；`Q::test_normal_operation_logs_never_contain_diary_text`：正常操作服务日志不含正文；**BUG-2 修复后**数据库错误日志也不含 | 通过（修复后） |
| 全量导出完整且不含凭据 | `A::test_audit_privacy_and_complete_export`：`counts` 与内容一致（2 日记/3 修订/1 记忆/1 来源/1 人物/1 对话/审计自洽）；导出含软删除日记（`deleted_at` 非空）、原文、全部修订、已确认记忆 + 证据、人物、对话消息（占位模式）；`Content-Disposition: attachment`、`Cache-Control: no-store`；不含 `test-admin-token` / `qa-api-token` / `ai_api_key` | 通过 |
| 备份脚本可用（生成校验/manifest） | `A::test_backup_and_restore_scripts_roundtrip`（in-process 调用 CLI `main()`）：`integrity_check=ok`、`.sha256`/`.json` manifest 正确、表行数正确；`Q::test_backup_manifest_and_tampered_restore_are_rejected`：拒绝覆盖已有备份、篡改 `.sha256` 后恢复校验拒绝、还原真实校验和后通过 | 通过 |
| 恢复脚本可用（数据完整可读） | `A::test_backup_and_restore_scripts_roundtrip`：`restore.main()` 恢复到新 SQLite 后新建 app 读取，原文 + 两版修订完整 | 通过（仅空闲临时库，见第 7 节） |
| 错误响应不泄漏堆栈/SQL/日记文本 | `A::test_requirement_09`、`A::test_requirement_10`（错误响应无 `Traceback`、不回显私密输入）；`Q::test_database_error_handler_never_logs_sql_or_diary_text`（500 响应通用、日志无 SQL/正文）；`A::test_audit_privacy_...` | 通过（修复后） |
| 迁移测试仍为绿色 | 既有 `X::test_migrations.py::test_alembic_upgrade_head_creates_schema_and_guards` 在全量套件中通过（alembic upgrade head 建表 + 触发器 + alembic_version） | 通过 |
| 跨测试隔离（不碰真实 DB） | `A::test_suite_uses_isolated_temporary_database`（settings 的 DB 路径位于 `tmp_path`、data/backup 目录位于 tmp、且不等于项目 `data/heartlog.db`）；QA 在多次全量运行前后检查：`data/` 仅 `.gitkeep`、`backups/` 为空、项目内无套件新建的 `*.db`/`-wal`/`-shm`；既有 `X::test_migrations.py` 也使用临时库 | 通过 |
| 安全/稳健性对抗 | `Q::test_pathological_ids_and_sql_injection_are_handled`（SQL 注入样式 ID 与检索词、超长/中文/`%`/路径穿越式 ID 均 4xx 且无 5xx，数据表仍可用）；`Q::test_api_cannot_mass_assign_immutable_diary_fields`（不可变字段批量赋值 422）；`Q::test_no_api_path_can_create_a_sourceless_memory`；`Q::test_editing_soft_deleted_diary_is_rejected_without_writes`（软删除后编辑 404 且不新增修订） | 通过 |
| 其它分页/参数边界 | `Q::test_misc_validation_edges_return_4xx`（date_from>date_to、对话标题/消息、记忆过滤/分页、更正空内容） | 通过 |
| 测试套件卫生 | 新增测试仅使用临时 DB/进程内 fake，无网络、无 sleep、无真实服务器；新增测试文件 ruff check/format 通过 | 通过（发现既有套件的 1 个日志状态污染，见 6.3） |

---

## 6. 发现的其它问题 / 残留风险（未被要求修复，或超出 QA 写权限）

> 以下不是用户 20 项行为的失败，而是独立 QA 过程中发现的文档/UX/测试卫生层面的真实缺口，供 Lead 决策；qa-dev 未修改任何源码或文档。

### 6.1 永久删除在"曾经提取过记忆"的日记上无法通过 API 完成

- 现象（临时库实测）：创建日记 → 提取记忆 → 永久删除返回 `409 entry_has_references`；随后 `POST /memories/{id}/revoke`（200，revoked）→ 再次永久删除**仍**返回 `409 entry_has_references`。
- 原因：`DiaryRepository.count_references()` 统计所有 `memories.source_entry_id` 行（含 revoked/superseded），而 API 没有任何"记忆物理删除"端点（`DELETE /api/v1/memories/{id}` → 405）。
- 与文档的出入：`docs/operations.md` 的排障表建议"先撤回并处理依赖记录，或使用软删除"，但"撤回"本身并不能解除永久删除阻塞。
- 影响：需求本身要求"被引用则拒绝永久删除"，因此**不违反 20 项需求**；但这是文档/UX 缺口：一旦提取过记忆，该日记只能软删除。建议文档明确"永久删除仅适用于从未被记忆/人物引用、或依赖行被手工清理的日记"。
- **Lead 最终处理（2026-10-29 收尾）**：已更新 `docs/operations.md`、`docs/privacy.md`、`docs/database_design.md`、README，明确"当前没有删除记忆 API，revoke 也不会解除引用阻塞；被引用日记只能软删除"。未新增记忆物理删除 API（保留引用完整性，记录为后续工作）。

### 6.2 软删除日记后，由它派生的记忆仍默认可见、证据片段仍可读取

- 现象：对日记软删除后，`GET /api/v1/memories` 默认列表仍返回其派生记忆；`GET /api/v1/search` 的 memory 命中仍可出现；`GET /api/v1/memories/{id}/sources` 仍返回该日记修订中的证据原文片段。
- 评估：这与 `docs/database_design.md`（"软删除：修订全部保留；记忆/人物引用不变"）以及 `docs/privacy.md`（软删除后修订与记忆保留）一致；且对话/回顾确实过滤已删除日记。**不是 bug**，但属于用户预期风险：用户可能认为"软删除日记"会隐藏派生内容。建议在隐私文档中显式提示"记忆/证据不会随日记软删除而隐藏，需撤回记忆"。
- **Lead 最终处理（2026-10-29 收尾）**：已在 `docs/privacy.md` 与 README 的已知限制中显式说明该语义；未改行为（保留记忆可追溯性），并列入后续"删除/遗忘工作流"。

### 6.3 既有测试的日志状态污染（测试卫生，非产品缺陷）

- 现象：`tests/test_migrations.py` 通过 `alembic/env.py` 的 `fileConfig(config.config_file_name)` 运行迁移；`fileConfig` 默认 `disable_existing_loggers=True`，会**禁用已存在的 `heartlog` logger**。在此之后运行任何日志隐私断言都会"因日志被禁用而空通过"（QA 在开发过程中实际遇到：同一 BUG-2 测试在单独运行 vs 全量运行下行为不同——全量时 XPASS）。
- QA 处理：`tests/test_qa_adversarial.py` 的 `log_collector` fixture 显式执行 `logger.disabled = False` 并在测试后恢复，使日志断言与执行顺序无关；同时断言"必须真的捕获到 database_error 日志"以防假阳性。
- 建议（未修改，超出 QA 文件所有权）：在 `alembic.ini` 的 logging 配置中设置 `disable_existing_loggers = false`，或在 `test_migrations.py` 结束前恢复 logging 配置。
- **Lead 最终修复（2026-10-29 收尾）**：`alembic/env.py` 改为 `fileConfig(..., disable_existing_loggers=False)`，迁移不再禁用应用 logger；修复后全量套件仍为 172 passed，QA 的防假阳性日志断言保留。

### 6.4 新增加的控制字符校验尚未写入文档

- 修复 BUG-1 后，`DiaryCreate`/`DiaryUpdate` 会拒绝 NUL、除 `\t \n \r` 外的 C0 控制字符与 DEL（422 `validation_error` / 服务层 `invalid_content`）。
- `docs/api.md` 的创建/编辑日记章节（以及 README 的校验说明）尚未提及该规则；建议 docs 维护者补充，避免文档与实现不完全一致。
- **Lead 最终处理（2026-10-29 收尾）**：已在 `docs/api.md` 与 README 验证说明中补充 NUL/控制字符校验规则，文档与实现已一致。

### 6.5 并发映射的边界

- BUG-3 修复只把 `OperationalError` 中消息包含 `locked`/`busy` 的情况映射为 409；其它数据库错误仍返回 500（合理）。
- SQLite `busy_timeout=30s`，若单次写锁竞争超过该超时仍可能得到 500；本次仅验证 2 线程并发与确定性陈旧快照场景，未做高并发压力测试（见第 7 节）。

### 6.6 其它观察

- 项目根目录存在队友遗留的 `.tmp*`/`pip-*` 目录与 `.tmp2/alembic.db`（均为 `.gitignore` 覆盖的本地 helper，mtime 早于 QA 全量运行，非测试套件产物）；`data/`、`backups/` 保持干净。
- 既有测试 `test_ai_service.py` 中存在对提示词文件内容的安全规则断言；QA 未修改提示词。

---

## 7. 明确未验证的内容（诚实声明）

以下内容本次 QA **没有**验证，不能视为通过：

1. **真实外部 AI 提供方**：全程离线，只验证了 AI 关闭路径、`HEARTLOG_AI_PROVIDER=mock` 的离线行为以及进程内 fake；没有真实 API Key、没有真实模型输出、没有验证真实 provider 的解析/超时/限流行为。
2. **真实 uvicorn / 生产部署**：没有启动常驻服务器，http 层全部通过 Starlette `TestClient`（ASGI 进程内）。"重启后数据仍在"通过同一 SQLite 文件上新建 app 实例验证，未通过操作系统级 kill/重启进程验证（覆盖了同一代码路径，但未覆盖进程生命周期、端口、反向代理等）。
3. **浏览器/UI**：项目无前端 UI；未做浏览器交互、CORS、`/docs` Swagger UI 的实际点击验证。
4. **多进程 / 高并发 / 压力测试**：仅做 2 线程 HTTP 并发与确定性陈旧会话场景；未做多进程、线程池高并发、长时间运行、SQLite 锁竞争压力测试。
5. **崩溃/损坏恢复**：未测试进程崩溃中断事务、磁盘满、SQLite 文件损坏、WAL 异常等场景；未验证 Alembic `downgrade` 往返（只跑了 `upgrade head`）。
6. **运行中备份**：备份脚本通过 in-process CLI 在空闲临时库上验证；未验证服务持续写入时在线备份的行为（虽然脚本使用 SQLite online backup API）。
7. **真实数据文件**：按任务要求从未打开/修改 `data/heartlog.db` 与 `backups/`；因此未在真实用户数据上做迁移/备份/恢复演练。
8. **安全审计深度**：只覆盖任务列出与 QA 自行设计的攻击面（NUL/控制字符、日志泄漏、SQL 注入样式输入、恶意 ID、批量赋值、伪造来源、令牌分支、导出凭据）；未做 fuzzing、认证绕过、TLS/反向代理、CSRF、依赖 CVE 等系统安全审计。
9. **既有 139 个测试的可信度**：这些测试由实现方编写；QA 新增 33 个独立测试用于交叉验证，但无法证明不存在未被覆盖的边界。

---

## 8. 结论

- 最终全量套件：**172 passed, 0 failed, 0 skipped, 1 条既有弃用警告，22.56s（退出码 0）**（`pytest tests -q`）。
- 用户要求的 20 项行为均有独立、可执行的测试证据，全部通过；QA 期间发现的 3 个真实缺陷（NUL→500、数据库错误日志泄漏正文、陈旧并发→500）已由 Lead 修复并由回归测试锁定。
- API 面、AI 关闭诚实性、令牌保护、永久删除保护、审计/日志隐私、完整导出、备份/恢复、迁移、测试隔离等附加检查均通过（详见第 5 节）。
- 主要残留风险：6.1 永久删除被历史记忆永久阻塞（已文档化，保留为后续工作）、6.2 软删除不隐藏派生记忆（已文档化）、6.5 高并发未压测，以及第 7 节列出的未验证范围。6.3 测试日志污染与 6.4 文档缺口已由 Lead 在收尾时修复。

---

## 9. 后续扩展与再验证（Lead，2026-10-29 晚）

本报告记录的是 v0.1 MVP 的独立 QA 快照（172 passed）。此后 Lead 按用户要求新增了 v0.2 能力，并重新运行了全量测试：

- **记忆遗忘**：`DELETE /api/v1/memories/{id}?mode=purge|anonymize`（确认头，删除内容/证据，只留无内容审计）；`DELETE /api/v1/persons/{id}`；`GET /api/v1/diaries/{id}/references`；软删除日记的派生记忆/人物默认隐藏，支持 `include_deleted_sources=true` 显式查看；搜索/回顾同步过滤。新增 `tests/test_forgetting.py` 9 个测试。
- **备份产品化**：AES-256-GCM + Scrypt 加密、口令环境变量、保留策略、`backup_scheduler.py`、`restore_drill.py`、加密恢复；新增 `tests/test_backup_productization.py` 18 个测试。
- **部署安全**：应用内滑动窗口限流（默认关闭，生产可开）、429/headers、可信代理配置；TLS/Nginx/Caddy/systemd 参考配置；开发证书与 OS 权限脚本；新增 `tests/test_rate_limit.py` 30 个测试。
- **最终全量**：`pytest tests -q` → **229 passed, 0 failed, 0 skipped, 1 warning in 29.23s（最近一次运行；多次运行约 28–29s）**（QA 报告中的 172 是 v0.1 历史快照，不是当前结果）。
- **仍不改变本报告的独立结论**：真实外部 AI、真实生产 TLS/代理/systemd 联调、多 worker 压测、真实灾难恢复演练仍未验证；上述 v0.2 配置的“参考示例未实测”限制继续适用。
