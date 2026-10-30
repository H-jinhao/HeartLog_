# HeartLog（心迹）

> 本地优先（local-first）的情绪日记 + 可追溯长期记忆后端。
> **原文不可变；每一次编辑都成为一条新修订；没有来源的记忆不会存在；AI 默认关闭。**

HeartLog 是一个运行在你自己的电脑上的个人情绪日记服务，目标不是"让 AI 假装记得你"，而是：

1. 忠实地保存你写下的原始文字；
2. 允许你不断补充、修改当天的记录，但永远不覆盖旧版本；
3. 在你**明确确认**的前提下，把日记中可引用的原文片段沉淀为长期记忆；
4. 每一条记忆都能回指到产生它的那一条日记修订和那段原文证据；
5. 只有你主动开启并配置后，才可能把必要数据发送给外部 AI 提供方；
6. 支持可审计的删除/遗忘：可以永久移除记忆与证据、在引用归零后物理删除日记，并默认隐藏已软删除日记的派生数据。

当前版本：`0.1.0`（开发中，单用户、本机使用；尚未经过生产级安全审计）。本轮已并入 v0.2 增量能力：记忆遗忘/人物遗忘、派生数据默认隐藏、加密/保留/定时备份与恢复演练、应用内限流与部署安全参考配置；最终测试与验证边界见文末[验证状态](#验证状态最终)，版本号尚未提升。

---

## 目录

- [本轮新增能力（v0.2 增量）](#本轮新增能力v02-增量)
- [核心事实完整性规则（不可妥协）](#核心事实完整性规则不可妥协)
- [快速开始](#快速开始)
  - [通用环境（Linux / macOS / WSL）](#通用环境linux--macos--wsl)
  - [当前 Windows 开发工作区](#当前-windows-开发工作区)
  - [数据库迁移](#数据库迁移)
  - [启动服务](#启动服务)
  - [运行测试](#运行测试)
- [前端（Web UI）](#前端web-ui)
- [目录结构](#目录结构)
- [配置（`.env`）](#配置env)
- [AI 是可选项：启用前请务必阅读](#ai-是可选项启用前请务必阅读)
- [备份、恢复与导出](#备份恢复与导出)
- [部署与安全（限流 / TLS / 反向代理）](#部署与安全限流--tls--反向代理)
- [API 最小示例](#api-最小示例)
- [已知限制](#已知限制)
- [后续计划](#后续计划)
- [验证状态（最终）](#验证状态最终)

更多细节见：

| 文档 | 内容 |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | 组件与请求流、原文/修订分离、记忆提取与审核流水线、遗忘流程、限流中间件、事务与错误处理 |
| [docs/database_design.md](docs/database_design.md) | 全部表、字段、约束、索引、SQLite 触发器、哈希链、删除/遗忘语义、迁移流程 |
| [docs/privacy.md](docs/privacy.md) | 本地优先默认值、会发送什么给 AI、日志策略、遗忘语义、备份加密、威胁模型 |
| [docs/api.md](docs/api.md) | 端点参考与真实请求/响应示例、错误码、分页参数、429 限流响应 |
| [docs/operations.md](docs/operations.md) | 迁移、备份（加密/保留/定时/演练）、校验、恢复、导出、限流配置、令牌轮换、故障排查 |
| [docs/backup.md](docs/backup.md) | 备份产品化：AES-256-GCM 加密、密钥管理、保留策略、定时任务、恢复演练 |
| [docs/deployment_security.md](docs/deployment_security.md) | 部署形态、TLS、Nginx/Caddy/systemd、限流、OS 权限加固、部署威胁模型 |
| [deploy/README.md](deploy/README.md) | 参考配置（Nginx / Caddy / systemd）与安装步骤（**示例，未在真实环境联调**） |
| [frontend/README.md](frontend/README.md) | React Web UI 的运行方式、设计系统、演示模式与交互说明 |

---

## 本轮新增能力（v0.2 增量）

| 能力 | 入口 | 说明 |
| --- | --- | --- |
| 记忆遗忘（purge / anonymize） | `DELETE /api/v1/memories/{memory_id}?mode=purge\|anonymize` + `X-Confirm-Memory-Delete` | **删除**记忆行与它的全部证据来源，只留下不含内容的追加式审计事件（`memory.purged` / `memory.anonymized`）。两种模式删除的内容相同，仅审计事件类型不同。 |
| 人物遗忘 | `DELETE /api/v1/persons/{person_id}` + `X-Confirm-Person-Delete` | 永久删除人物行，仅留无内容审计（`person.purged`）。 |
| 日记引用检查 | `GET /api/v1/diaries/{diary_id}/references` | 返回 `memories` / `memory_sources` / `persons` 引用计数与 `can_permanently_delete`，用于判断永久删除是否仍被阻塞（软删除的日记也可查询）。 |
| 解除日记永久删除阻塞 | 先 purge/anonymize 引用该日记的**全部**记忆与人物，再 `DELETE /api/v1/diaries/{id}/permanent` | 遗忘会删除记忆/证据/人物行，因此引用归零后管理员才能物理删除日记（含全部修订）。 |
| 派生数据默认隐藏 | `include_deleted_sources=true` 显式 opt-in | 来源日记被软删除后，派生记忆与人物在 `GET /memories`、记忆详情/来源、`GET /persons`、人物详情/时间线、`/search` 的 memory 命中中**默认隐藏**（传 `include_deleted_sources=true` 可显式查看，响应 `source_entry_deleted` 标明来源已删除）；**周/月回顾始终排除这类派生记忆，没有 opt-in**。按 id 的定向操作（遗忘、审阅）仍可用。 |
| 加密备份 | `python scripts/backup.py --encrypt` | AES-256-GCM + Scrypt 口令派生；口令只从进程环境变量 `HEARTLOG_BACKUP_PASSPHRASE` 读取（不是 Settings / `.env`），丢失即不可恢复。默认仍是明文备份。 |
| 备份保留策略 | `--retention N` | 只删除 HeartLog 自动命名、且 `.sha256`/manifest 自洽的旧备份；手动命名文件永不自动删除。 |
| 定时备份 | `python scripts/backup_scheduler.py --once` / `--interval-hours H` | 本地定时/前台循环，不依赖外部调度库；只记录元数据日志。 |
| 恢复演练 | `python scripts/restore_drill.py <backup> [--json]` | 只读校验备份可恢复性（哈希、解密、SQLite 完整性、日记/记忆业务不变量），不触碰在线数据库。 |
| 应用内限流 | `HEARTLOG_RATE_LIMIT_*` | `/api/v1` 的进程内滑动窗口限流，默认关闭；超限返回 `429 rate_limited` + `Retry-After` / `X-RateLimit-*`。 |
| 部署安全参考 | `deploy/nginx.conf`、`deploy/Caddyfile`、`deploy/heartlog.service`、`scripts/generate_dev_cert.py`、`scripts/harden_permissions.py` | TLS/反向代理/systemd 加固/权限收紧的**参考示例**，**未**在真实 Nginx/Caddy/systemd 环境联调，详见 [docs/deployment_security.md](docs/deployment_security.md)。 |

> 诚实边界：本轮新增的加密备份、限流与部署配置由自动化测试覆盖代码路径；**真实加密备份的恢复、真实 Nginx/Caddy/systemd 联调、真实外部 AI 调用均未在本仓库环境中验证**。备份加密是**可选**的（默认仍生成明文数据库）；遗忘/删除不会清理历史备份与导出（见 [docs/privacy.md](docs/privacy.md#6-删除语义你删的到底是什么)）。

---

## 核心事实完整性规则（不可妥协）

这些规则由数据库约束、SQLAlchemy 事件监听器、SQLite 触发器和业务服务共同实现，不是"约定"：

1. **原始文本只写一次（immutable original）。** 创建日记时保存 `original_text` 及其 SHA-256；此后任何更新都不能修改原文、原文哈希和创建时间。ORM 监听器会抛 `ProtectedRecordError`，SQLite 触发器会让裸 SQL 更新 `RAISE(ABORT)`。见 [`app/models/diary.py`](app/models/diary.py) 与 [`app/db/guards.py`](app/db/guards.py)。
2. **编辑 = 追加修订（append-only revisions）。** `PUT /diaries/{id}` 不覆盖当前内容，而是插入 `version = current_version + 1` 的新修订，并记录 `previous_revision_hash` 形成哈希链；旧修订永久保留。`DiaryRevision` 的更新会被 ORM 和触发器双重拒绝。
3. **没有来源，就没有记忆（no source, no memory）。** 每条记忆必须指向一条日记条目 + 一条具体修订；证据 `evidence_text` 必须是该修订正文的**逐字符精确子串**，给出的 `start_offset/end_offset` 也必须与切片完全一致。伪造证据的候选会被拒绝，且不写入任何行。见 [`app/services/memory_service.py`](app/services/memory_service.py)。
4. **AI 置信度不能替代用户确认。** 模型提取的记忆一律以 `pending`、`created_by="model"` 落库；即使 `confidence = 0.99` 也不会自动确认。确认动作只能由用户通过 `POST /memories/{id}/confirm` 完成，且**永远不会把 `model_inference` 改成 `explicit_fact`**。
5. **事实、感受、信念、推断严格分离。** 记忆类型固定为 `explicit_fact` / `subjective_feeling` / `explicit_belief` / `model_inference` 四种，数据库有 CHECK 约束；推断只能以推断口吻存在。
6. **历史只追加。** 更正（correct）不是改写旧记录，而是新建一条 `source_type="user_correction"` 的记忆并让旧记忆变为 `superseded`；撤回（revoke）只改变状态并隐藏于常规列表，仍可按 id 追溯。
7. **矛盾保留。** 同一条日记里或不同日记之间的冲突说法不会被自动合并或"选一个正确版本"；它们作为不同记忆并存。
8. **审计只追加、只记结构信息。** `audit_events` 禁止 UPDATE/DELETE（ORM + 触发器），且只记录 ID、版本、数量、理由等结构字段，不复制日记正文。
9. **删除分层，且默认不物理删除。** 正常删除是软删除（可恢复、修订全保留）；物理删除必须同时满足：配置了 `HEARTLOG_ADMIN_TOKEN`、携带管理员令牌、携带明确的二次确认头、且该日记没有被任何记忆/人物/证据引用。引用可以用**遗忘**接口归零：`DELETE /memories/{id}`（purge/anonymize）会在删除记忆内容的同时删除其证据，`DELETE /persons/{id}` 删除人物行；两者都只留下不含内容的追加式审计事件。见 [docs/api.md](docs/api.md) 与 [docs/operations.md](docs/operations.md)。
10. **AI 默认关闭且需要显式同意。** `HEARTLOG_AI_ENABLED=true` **与** `HEARTLOG_AI_CONSENT=true` 同时为真、且提供方配置完整时，才可能发起外部网络请求；否则不会调用任何模型（见下文与 [docs/privacy.md](docs/privacy.md)）。
11. **派生数据跟随来源的可见性。** 来源日记被软删除后，它的记忆与人物在默认列表/详情/搜索中隐藏；只有显式传 `include_deleted_sources=true` 才会看到（定向删除/审阅按 id 仍可用），而周/月回顾**始终**排除这类派生记忆。这既不是"联级删除"，也不是"忘记"：派生行与证据仍在数据库中，直到你显式 purge。见 [docs/privacy.md](docs/privacy.md#6-删除语义你删的到底是什么)。

> 诚实边界：哈希用于**发现**意外或静默修改、并让修订链可验证，但它不是加密、也不是不可篡改存储。拥有数据库文件写权限的人可以删掉触发器后改数据，或直接用备份覆盖。详见 [docs/privacy.md](docs/privacy.md)。

---

## 快速开始

要求：**Python >= 3.11**（`pyproject.toml` 中 `requires-python = ">=3.11"`）。

依赖清单在 [`requirements.txt`](requirements.txt)，与 [`pyproject.toml`](pyproject.toml) 的 `dependencies` 一致：

```
fastapi==0.162.0
uvicorn==0.54.0
SQLAlchemy==2.0.55
pydantic==2.14.1
pydantic-settings==2.16.2
alembic==1.20.1
httpx==0.28.1
python-dotenv==1.2.4
cryptography==46.0.7   # 加密备份 / 开发证书脚本
pytest==9.2.0   # 开发/测试
ruff==0.17.3    # 开发/测试
```

### 通用环境（Linux / macOS / WSL）

```bash
cd heartlog
python -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt      # 或: pip install -e ".[dev]"

cp .env.example .env                 # 仓库提供模板；按需修改，.env 已被 .gitignore 忽略
python -m alembic upgrade head       # 建表 + 安装 SQLite 完整性触发器
uvicorn "app.main:create_app" --factory --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/docs> 查看 Swagger UI（OpenAPI: `/openapi.json`，ReDoc: `/redoc`）。
服务默认只建议监听 `127.0.0.1`；如果要监听 `0.0.0.0`，请先阅读 [docs/privacy.md](docs/privacy.md) 的安全说明与 [docs/deployment_security.md](docs/deployment_security.md) 的部署指南（TLS / 反向代理 / 认证 / 限流；**不要**直接把 uvicorn 暴露到公网）。

### 当前 Windows 开发工作区

本机（开发机）依赖已解包到工作区的 `.deps`，并使用一个针对 Windows ACL 的本地 shim `.pyshim`。这不是 HeartLog 的正常安装方式，只用于当前工作区：

```powershell
cd D:\Harness\heartlog
$env:PYTHONPATH = 'D:\Harness\heartlog\.pyshim;D:\Harness\heartlog\.deps;D:\Harness\heartlog'
& 'C:\Users\H\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe' -m alembic upgrade head
& 'C:\Users\H\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe' -m uvicorn "app.main:create_app" --factory --host 127.0.0.1 --port 8000
```

Git Bash 中使用同样的 `PYTHONPATH`（用单引号避免转义）：

```bash
export PYTHONPATH='D:\Harness\heartlog\.pyshim;D:\Harness\heartlog\.deps;D:\Harness\heartlog'
```

> 说明：`.deps/` 是本地已解包的依赖目录，`.pyshim/` 是这台机器上绕过 ACL 限制的 sitecustomize shim；两者都在 `.gitignore` 中，也不属于项目要求。在正常环境里用虚拟环境安装依赖即可。

### 数据库迁移

- 初始化/升级到最新：`python -m alembic upgrade head`
- 查看当前版本：`python -m alembic current`
- 回退一个版本：`python -m alembic downgrade -1`
- 迁移文件：[`alembic/versions/1b179ca3e706_initial_schema.py`](alembic/versions/1b179ca3e706_initial_schema.py)（初始 schema + 完整性触发器）

> ⚠️ 不要对同一个数据库混用两条建表路径：设置 `HEARTLOG_AUTO_CREATE_SCHEMA=true`（默认值）时应用启动会用 `Base.metadata.create_all()` 兜底建表，但**不会**写入 `alembic_version`。如果先用应用启动建了表，再执行 `alembic upgrade head` 会因表已存在而失败。推荐流程：**新库先 `alembic upgrade head`，再启动应用**；`auto_create_schema` 主要方便临时库/测试。

### 启动服务

必须使用**应用工厂**方式启动（模块导入时不触碰数据库）：

```bash
uvicorn "app.main:create_app" --factory --host 127.0.0.1 --port 8000
```

健康检查：`GET /health`（不受 API Token 保护）。启动/停止时日志会记录 `heartlog_start` / `heartlog_stop`，停止时释放数据库引擎。

### 运行测试

```bash
# 通用环境
pytest -q

# 当前 Windows 工作区
$env:PYTHONPATH = 'D:\Harness\heartlog\.pyshim;D:\Harness\heartlog\.deps;D:\Harness\heartlog'
& 'C:\Users\H\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe' -m pytest -q
```

测试使用 `tmp_path` 下的临时 SQLite 数据库（见 [`tests/conftest.py`](tests/conftest.py)），不会读写真实的 `data/heartlog.db`。外部 AI 不会在测试中被调用。

> 本 README 的测试数量与结果来自 Lead 收尾时实际运行，见文末[验证状态](#验证状态最终)。

---

## 前端（Web UI）

仓库包含一个独立的 React + TypeScript 前端，位于 [`frontend/`](frontend/)。它只通过 `/api/v1`、`/health` 访问后端，不修改任何后端表结构或业务规则。

```bash
cd frontend
npm install
npm run dev        # http://127.0.0.1:5173
```

开发服务器默认把 `/api`、`/health`、`/docs`、`/openapi.json` 代理到 `http://127.0.0.1:8000`，因此后端不需要额外开启 CORS。

- 设计与交互说明：[frontend/README.md](frontend/README.md)
- 构建检查：`npm run typecheck && npm run build`
- 后端离线时自动进入**明确标注的演示模式**；演示数据只存在于当前会话，不会写入后端。真实保存/确认/导出只有收到成功响应才会提示成功。
- 当前页面：Dashboard、日记列表/详情/修订、长期记忆/人物/时间线、周月情绪分析、我的/隐私/导出。
- PWA：构建产物包含 manifest、Service Worker 和主屏幕图标；同一 Wi-Fi 下可用 `npm run preview:lan` 在手机安装/添加到主屏幕。完整离线安装需要 HTTPS。
- 技术栈：React 19、Vite 6、TypeScript、Tailwind CSS、Motion、Lucide React、React Router、vite-plugin-pwa。

> 前端不包含账号体系或云端同步；AI 状态由后端环境变量控制，前端不保存模型密钥。

---

## 目录结构

```
heartlog/
├── app/
│   ├── main.py                # create_app() 应用工厂、中间件、异常处理器、日志
│   ├── core/                  # 配置、异常、哈希、可选令牌安全、限流、UTC 时间
│   ├── db/                    # Base/mixins、引擎与会话、UTCDateTime、SQLite 触发器
│   ├── models/                # SQLAlchemy 模型：日记/修订/记忆/来源/人物/审计/会话
│   ├── schemas/               # Pydantic 请求/响应模型（含 AI 结构化输出 schema）
│   ├── services/              # 业务规则与事务：日记、记忆、搜索、人物、导出、AI、对话、回顾、备份原语
│   ├── repositories/          # 只读/写入查询（SQLAlchemy），不持有事务
│   ├── api/
│   │   ├── router.py          # /api/v1 路由汇总 + 可选 Bearer 鉴权
│   │   ├── deps.py            # 依赖注入：设置、DB Session、AIService
│   │   └── routes/            # health / diaries / memories / search / persons /
│   │                          # conversations / insights / export
│   └── prompts/               # 可审阅的 Markdown 提示词（提取/陪伴/回顾）
├── alembic/                   # 迁移环境与版本脚本
├── scripts/                   # backup / restore / backup_scheduler / restore_drill /
│                              # verify_integrity / export_data / generate_dev_cert / harden_permissions
├── deploy/                    # Nginx / Caddy / systemd 参考配置与部署说明（示例，未联调）
├── tests/                     # pytest 测试（临时数据库，不访问外网）
├── frontend/                  # React + TypeScript Web UI（独立 npm 项目）
│   ├── src/                   # 页面、组件、API 客户端、演示数据
│   ├── README.md              # 前端运行、设计系统与交互说明
│   └── vite.config.ts         # 开发/预览同源代理
├── docs/                      # 设计、数据库、隐私、API、运维、备份、部署安全文档
├── data/                      # 默认 SQLite 数据库目录（私有，已被 gitignore）
├── backups/                   # 默认备份目录（私有，已被 gitignore）
├── .env.example               # 配置模板（复制为 .env 使用）
├── requirements.txt / pyproject.toml
└── README.md
```

分层原则：`routes` 只做传输层校验 → `schemas` 定义契约 → `services` 持有业务规则与事务 → `repositories` 执行查询 → `models` 定义持久化与数据库级保护。详见 [docs/architecture.md](docs/architecture.md)。

---

## 配置（`.env`）

配置通过环境变量或工作目录下的 `.env` 文件加载，前缀固定为 `HEARTLOG_`，大小写不敏感，未知键忽略。字段定义以 [`app/core/config.py`](app/core/config.py) 为准。主要配置：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HEARTLOG_APP_NAME` | `HeartLog` | 应用显示名 |
| `HEARTLOG_ENVIRONMENT` | `local` | `local` / `test` / `staging` / `production` |
| `HEARTLOG_DEBUG` | `false` | 调试开关（当前仅用于部署告警提醒，未开启 FastAPI debug 行为） |
| `HEARTLOG_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` |
| `HEARTLOG_DATABASE_URL` | `sqlite:///./data/heartlog.db` | SQLAlchemy URL；SQLite 相对路径相对于启动工作目录 |
| `HEARTLOG_DATA_DIR` | `./data` | 数据目录（启动时自动创建） |
| `HEARTLOG_BACKUP_DIR` | `./backups` | `scripts/backup.py` 的默认输出目录（启动时自动创建） |
| `HEARTLOG_AUTO_CREATE_SCHEMA` | `true` | 启动时 `create_all()` 兜底建表（见上文迁移警告） |
| `HEARTLOG_API_V1_PREFIX` | `/api/v1` | 版本化 API 前缀，必须以 `/` 开头 |
| `HEARTLOG_API_TOKEN` | 空 | 设置后所有 `/api/v1` 请求必须带 `Authorization: Bearer <token>` |
| `HEARTLOG_ADMIN_TOKEN` | 空 | 设置后才允许**日记物理删除**；未设置时该操作整体禁用（记忆/人物遗忘只需确认头，不需要管理员令牌） |
| `HEARTLOG_MAX_DIARY_CHARS` | `50000` | 日记正文最大长度（服务层校验） |
| `HEARTLOG_MAX_CHANGE_REASON_CHARS` | `500` | 修订理由最大长度 |
| `HEARTLOG_DEFAULT_PAGE_SIZE` | `20` | 默认分页大小 |
| `HEARTLOG_MAX_PAGE_SIZE` | `100` | 单页上限（服务层会取 `min(limit, 该值)`） |
| `HEARTLOG_MAX_EVIDENCE_CHARS` | `2000` | 证据片段最大长度；同时限制记忆内容长度（`min(4000, 该值)`） |
| `HEARTLOG_RATE_LIMIT_ENABLED` | `false` | 应用内 `/api/v1` 限流总开关（默认关闭，不影响本地单机使用） |
| `HEARTLOG_RATE_LIMIT_REQUESTS` | `240` | 滑动窗口内允许的请求数（1–1,000,000） |
| `HEARTLOG_RATE_LIMIT_WINDOW_SECONDS` | `60` | 滑动窗口长度（秒，1–86,400） |
| `HEARTLOG_RATE_LIMIT_EXEMPT_PATHS` | `/health,/docs,/redoc,/openapi.json` | 逗号分隔的免限流路径前缀（按路径或子路径匹配） |
| `HEARTLOG_TRUST_PROXY_HEADERS` | `false` | 是否把 `X-Forwarded-For` 第一跳作为限流键；**仅**在可信反向代理且代理"覆盖写"该头时开启 |
| `HEARTLOG_AI_ENABLED` | `false` | AI 功能总开关（默认关闭） |
| `HEARTLOG_AI_CONSENT` | `false` | 显式同意；`AI_ENABLED=true` 但此项为 false 时应用**拒绝启动** |
| `HEARTLOG_AI_PROVIDER` | `disabled` | `disabled` / `openai_compatible` / `mock` |
| `HEARTLOG_AI_BASE_URL` | 空 | OpenAI 兼容服务的 base URL（如 `https://api.example.com/v1`） |
| `HEARTLOG_AI_MODEL` | 空 | 模型名 |
| `HEARTLOG_AI_API_KEY` | 空 | API Key（`SecretStr`，不会被导出/日志记录） |
| `HEARTLOG_AI_TIMEOUT_SECONDS` | `60` | 外部请求超时（1–600） |
| `HEARTLOG_AI_MAX_OUTPUT_TOKENS` | `2000` | 生成上限（64–32000） |

行为说明：

- `.env`、`data/`、`backups/`、`exports/`、`*.db` 均已在 [`.gitignore`](.gitignore) 中忽略；只有 `.env.example` 允许提交。
- 没有密钥拥有可用的默认值；不配置 AI 时系统完全离线可用（除浏览器打开 `/docs` 会加载 CDN 静态资源这一前端行为）。
- 修改配置后需要重启服务（配置在进程启动时加载）。
- **备份口令不是 Settings 字段**：`HEARTLOG_BACKUP_PASSPHRASE` 由备份/恢复脚本直接读进程环境变量，写进项目 `.env` 不会被读取；不要把它写进命令行参数、git 或日志（见 [docs/backup.md](docs/backup.md#42-口令从哪来)）。
- `HEARTLOG_ENVIRONMENT=staging|production` 时，启动会以 `deployment_security` WARNING 输出缺失 API/管理员令牌、未开启限流、`trust_proxy_headers=true` 等提醒（不含密钥值，也不阻止启动），见 [docs/deployment_security.md](docs/deployment_security.md#13-生产预发布启动告警)。

---

## AI 是可选项：启用前请务必阅读

HeartLog 的默认状态是 **AI 完全关闭**：不会创建 provider 客户端、不会发起任何模型网络请求。

只有同时满足以下三个条件，才可能发生外部数据传输：

1. `HEARTLOG_AI_ENABLED=true`；
2. `HEARTLOG_AI_CONSENT=true`（缺少它应用会直接拒绝启动，避免"被默认开启"）；
3. `HEARTLOG_AI_PROVIDER` 配为 `openai_compatible`，且 `AI_BASE_URL`、`AI_MODEL`、`AI_API_KEY` 完整（或选择 `mock` 本地假提供方，它不联网）。

启用后，**可能**发送的数据（准确说明见 [docs/privacy.md](docs/privacy.md)）：

- **记忆提取**：目标日记修订的完整正文、`entry_id` / `revision_id` / `version`，以及对应提示词；
- **陪伴对话**：你发出的消息、最多 8 条相关记忆（含内容、来源 id 与证据片段）、最多 20 条最近对话历史、当前 UTC 时间；
- **周期回顾**：周期边界、统计数字、最多 30 条记忆片段与来源 id（**不发送日记原文**）。

不会发送：数据库文件、API Token、其他配置密钥、未启用的数据结构。

不启用 AI 时，相关接口的行为是**诚实降级**，不会假装是 AI 生成的：

- `POST /api/v1/memories/extract` → `503 ai_not_configured`，不写任何数据；
- `POST /api/v1/conversations/{id}/messages` → `200`，`mode="placeholder"`、`ai_used=false`，助手消息是本机固定提示文案；
- `GET /api/v1/insights/weekly|monthly` → `200`，`mode="local_statistics"`、`ai_used=false`，只返回真实本地统计，`narrative=null`。

> 重要：本地统计/占位回复**不是**心理治疗、医疗诊断或危机干预；如果你处于危险中，请联系当地紧急服务或可信任的专业支持。

---

## 备份、恢复与导出

所有命令都在项目根目录、并激活环境后执行（当前工作区需先设置上文 `PYTHONPATH`）。**备份/导出文件包含完整私人日记内容，默认是明文的；加密备份是可选能力（推荐在离开本机磁盘时使用），请自行加密存储并妥善保管。** 备份/恢复的产品化细节（加密格式、密钥管理、保留语义、定时任务、恢复演练）见 [docs/backup.md](docs/backup.md)；基础运维步骤见 [docs/operations.md](docs/operations.md#4-备份)。

```bash
# 1) 明文备份（MVP 兼容）：SQLite 在线备份 API + PRAGMA integrity_check
#    并生成 .sha256 与 .json manifest（含各表行数）
python scripts/backup.py
python scripts/backup.py --output /path/to/heartlog-backup.db   # 自定义路径（要求文件不存在）

# 2) 加密备份（AES-256-GCM；口令只放进程环境变量，不写命令行/`.env`）
export HEARTLOG_BACKUP_PASSPHRASE="$(pass show heartlog/backup)"   # 示例：从密码管理器读取
python scripts/backup.py --encrypt
python scripts/backup.py --encrypt --retention 14   # 成功后只保留 14 个最新自动命名备份

# 3) 校验哈希链、记忆来源与证据片段（只打印 ID/代码，不打印正文）
python scripts/verify_integrity.py

# 4) 恢复演练（只读：不碰在线数据库；加密备份需同样设置口令）
python scripts/restore_drill.py backups/heartlog-<UTC>.db.enc
python scripts/restore_drill.py backups/heartlog-<UTC>.db.enc --json

# 5) 导出全部用户数据为 JSON（含原文、全部修订、记忆、来源、人物、会话、审计）
python scripts/export_data.py
python scripts/export_data.py --output /path/to/export.json
# 或通过 API（会额外写入 data.exported 审计事件）：
curl -s http://127.0.0.1:8000/api/v1/export -o heartlog-export.json

# 6) 恢复（必须先停止服务；目标库已存在时需 --force；加密备份用同一口令环境变量）
python scripts/restore.py /path/to/heartlog-backup.db --force
python scripts/verify_integrity.py
```

安全行为（已由代码实现）：备份/导出脚本拒绝覆盖已存在的文件；备份后自动做 `PRAGMA integrity_check` 并写 SHA-256 与 manifest；恢复前校验 `.sha256`/manifest 与 `integrity_check`，若目标库存在则先生成安全备份并检查 WAL/SHM sidecar；加密备份解密的临时文件会清理。**口令丢失 = 加密备份永久无法恢复**（无密钥托管/恢复码）；**旧备份不会自动遗忘**，删除数据后需自行按保留策略或手动清理历史备份与导出。完整说明见 [docs/backup.md](docs/backup.md)。

---

## 部署与安全（限流 / TLS / 反向代理）

默认形态仍是**只监听 `127.0.0.1` 的单机服务**：不内置 TLS、没有登录会话、没有多用户隔离。本轮新增的是**可选**的加固能力：

| 能力 | 怎么用 | 诚实边界 |
| --- | --- | --- |
| 应用内限流 | `.env` 设 `HEARTLOG_RATE_LIMIT_ENABLED=true`，按需调 `HEARTLOG_RATE_LIMIT_REQUESTS` / `HEARTLOG_RATE_LIMIT_WINDOW_SECONDS`；超限返回 `429 rate_limited` + `Retry-After` / `X-RateLimit-*` | 进程内滑动窗口：重启清零、多 worker 各自计数；不是 WAF，公网部署仍需代理层限流 |
| 信任代理头 | 仅在可信反向代理**覆盖写** `X-Forwarded-For` 时设置 `HEARTLOG_TRUST_PROXY_HEADERS=true` | 默认关闭；错误开启会让客户端可伪造限流键 |
| 本地 TLS（开发/单机） | `python scripts/generate_dev_cert.py` 生成自签证书，再以 uvicorn `--ssl-keyfile/--ssl-certfile` 启动 | 自签证书仅适合本机/测试；需客户端显式信任 |
| 生产 TLS / 反向代理 | 参考 [`deploy/nginx.conf`](deploy/nginx.conf) 或 [`deploy/Caddyfile`](deploy/Caddyfile)：TLS 1.2/1.3、代理层限流、可选 basic auth、默认隐藏 `/docs`、只记录 `$uri` 的访问日志 | **参考示例，未在真实 Nginx/Caddy/systemd 环境联调**；域名/证书/阈值均为占位值，上线前必须自行执行 `nginx -t` / `caddy validate` 并实测握手与 429 |
| systemd 常驻与加固 | [`deploy/heartlog.service`](deploy/heartlog.service) + [`docs/deployment_security.md`](docs/deployment_security.md) | 单元文件经人工审阅但未在真实 systemd 主机验证；`MemoryDenyWriteExecute` 等选项需按环境取舍 |
| 文件权限收紧 | `python scripts/harden_permissions.py`（默认 dry-run）→ `--apply` | Windows ACL 变更需相应权限；真实 ACL 变更未在本仓库环境实际执行 |

应用内限流与 TLS/反向代理的完整配置、验证命令、威胁模型与限制见 [docs/deployment_security.md](docs/deployment_security.md)；参考配置的安装步骤见 [deploy/README.md](deploy/README.md)。

---

## API 最小示例

以下示例假设服务运行在 `http://127.0.0.1:8000`，且未设置 `HEARTLOG_API_TOKEN`（若设置了，请为所有 `/api/v1` 请求加 `-H "Authorization: Bearer <token>"`）。完整字段与响应示例见 [docs/api.md](docs/api.md)。

```bash
# 创建日记（返回原始文本 + 当前内容 + 第 1 版修订）
curl -s -X POST http://127.0.0.1:8000/api/v1/diaries \
  -H "Content-Type: application/json" \
  -d '{"content":"今天她还是没有回复，我有点失落。","metadata":{"mood":"低"}}'

# 编辑：追加第 2 版，原始文本不变；expected_version 用于乐观并发保护
curl -s -X PUT http://127.0.0.1:8000/api/v1/diaries/<diary_id> \
  -H "Content-Type: application/json" \
  -d '{"content":"今天她还是没有回复，我有点失落。后来我去散步了。","expected_version":1,"change_reason":"补充后续"}'

# 读取全部历史版本 / 校验哈希链
curl -s http://127.0.0.1:8000/api/v1/diaries/<diary_id>/revisions
curl -s http://127.0.0.1:8000/api/v1/diaries/<diary_id>/integrity

# 关键词搜索（默认同时搜索日记修订与活跃记忆）
curl -s "http://127.0.0.1:8000/api/v1/search?q=%E5%A4%B1%E8%90%BD"

# AI 未启用时提取记忆：返回 503，不写入任何数据
curl -s -X POST http://127.0.0.1:8000/api/v1/memories/extract \
  -H "Content-Type: application/json" -d '{"entry_id":"<diary_id>"}'

# AI 启用并完成一次提取后，审核记忆（确认 / 更正 / 撤回）
curl -s -X POST http://127.0.0.1:8000/api/v1/memories/<memory_id>/confirm
curl -s -X POST http://127.0.0.1:8000/api/v1/memories/<memory_id>/correct \
  -H "Content-Type: application/json" -d '{"content":"更正后的记忆表述","change_reason":"更准确"}'
curl -s -X POST http://127.0.0.1:8000/api/v1/memories/<memory_id>/revoke \
  -H "Content-Type: application/json" -d '{"reason":"用户要求忘记"}'

# 遗忘：永久删除记忆内容与证据（purge 或 anonymize；不需要管理员令牌，只需确认头）
curl -s -X DELETE "http://127.0.0.1:8000/api/v1/memories/<memory_id>?mode=purge" \
  -H "X-Confirm-Memory-Delete: <memory_id>"
curl -s -X DELETE "http://127.0.0.1:8000/api/v1/memories/<memory_id>?mode=anonymize" \
  -H "X-Confirm-Memory-Delete: true"

# 检查日记的引用计数（判断永久删除是否仍被阻塞；软删除日记也可查）
curl -s http://127.0.0.1:8000/api/v1/diaries/<diary_id>/references

# 删除人物记录（永久删除行，仅留无内容审计；同样只需确认头）
curl -s -X DELETE http://127.0.0.1:8000/api/v1/persons/<person_id> \
  -H "X-Confirm-Person-Delete: <person_id>"

# 软删除日记后，派生记忆/人物默认从列表隐藏；显式 opt-in 才可见
curl -s "http://127.0.0.1:8000/api/v1/memories?include_deleted_sources=true"

# 陪伴对话：AI 未启用时返回明确标注的占位回复（mode=placeholder, ai_used=false）
curl -s -X POST http://127.0.0.1:8000/api/v1/conversations \
  -H "Content-Type: application/json" -d '{"title":"今天"}'
curl -s -X POST http://127.0.0.1:8000/api/v1/conversations/<conversation_id>/messages \
  -H "Content-Type: application/json" -d '{"content":"我今天有点难过"}'

# 周期回顾：AI 未启用时返回本地统计（mode=local_statistics）
curl -s "http://127.0.0.1:8000/api/v1/insights/weekly?date=2026-10-29"

# 导出全部数据（JSON 附件）
curl -s http://127.0.0.1:8000/api/v1/export -o heartlog-export.json
```

删除流程（分三层，详见 [docs/privacy.md](docs/privacy.md#6-删除语义你删的到底是什么) 与 [docs/api.md](docs/api.md)）：

1. `DELETE /api/v1/diaries/{id}` → **软删除**（可 `POST .../restore` 恢复；修订全保留；派生记忆/人物随之默认隐藏，但数据仍在）。
2. 对引用它的记忆执行 `DELETE /api/v1/memories/{id}?mode=purge|anonymize`、对人物执行 `DELETE /api/v1/persons/{id}` → **遗忘**（删除内容与证据/人物行，只留无内容审计）。
3. 引用归零后（`GET /api/v1/diaries/{id}/references` 的 `can_permanently_delete=true`），管理员才能 `DELETE /api/v1/diaries/{id}/permanent`（需 `X-Admin-Token` + `X-Confirm-Permanent-Delete`）物理删除日记及其全部修订。

> 以上删除/遗忘都**不会**清理历史备份、导出 JSON、文件系统快照或 WAL 残留；需要按备份保留策略/手动清理，见 [docs/backup.md](docs/backup.md#12-已知限制请纳入你的威胁模型)。

---

## 已知限制

- **单用户、无账号体系**。只提供可选的单一 Bearer Token 与管理员 Token，不是完整的认证/授权方案；没有用户注册、登录、会话管理、多租户隔离。
- **本机使用定位，TLS/认证/限流均为可选加固**。应用本身不内置 TLS、登录会话与多用户隔离；应用内限流默认关闭且是**进程内**实现（重启清零、多 worker 各自计数）。暴露到非 loopback 时应放在带 TLS、认证、代理层限流的反向代理之后；Nginx/Caddy/systemd 参考配置**未在真实环境联调**，详见 [docs/deployment_security.md](docs/deployment_security.md)。
- **SQLite 单一存储**。并发写受 SQLite 单写者模型限制（已开启 WAL、`busy_timeout=30000`）；不支持多进程/多主机水平扩展。尚未验证其他数据库的完整可运行性。
- **AI 目前是可选增强，不是核心依赖**。记忆提取依赖已配置的 AI 提供方；未配置时提取接口返回 503。对话与周期回顾在 AI 关闭时返回明确的本地占位/统计结果（真实 AI 模式依赖外部提供方的质量与稳定性；真实外部 AI 调用未在本仓库环境验证）。
- **没有向量检索 / 语义搜索**。搜索是透明的 SQL 子串匹配（`LIKE`，大小写不敏感），不会"猜"相关性。
- **没有前端 UI**。交互通过 HTTP API 与 `/docs`；仓库内没有 Web/桌面界面。
- **哈希不是加密，也不是不可篡改存储**。能写数据库文件的人可以删除触发器后改数据；数据库本身没有静态加密，备份默认也是明文（加密备份为可选能力）。
- **跨表不变量不是纯数据库约束**。"每条记忆至少有 1 条 `memory_sources` 证据行"、"证据是来源修订的精确子串"、"来源修订属于来源日记"等由服务层、校验脚本和测试保证；数据库层只强制主来源字段非空等部分约束（见 [docs/database_design.md](docs/database_design.md#8-已知限制数据库层面)）。
- **遗忘/删除不等于"彻底忘记"**。purge/anonymize 会删除记忆内容、证据与人物行，只留不含内容的追加式审计；但软删除日记的修订/原文、审计结构、**历史备份/导出/同步副本/文件系统快照**仍可能保留旧数据，SQLite 页面级安全擦除也不在保证范围内。备份中的历史数据不会被自动遗忘（见 [docs/backup.md](docs/backup.md#12-已知限制请纳入你的威胁模型)）。
- **派生数据"默认隐藏"不是删除**。来源日记软删除后，记忆/人物在默认列表、详情、搜索中隐藏（`include_deleted_sources=true` 可 opt-in），周/月回顾**始终**排除这类派生记忆；派生行与证据仍在库中，需显式 purge 才会删除。
- **被引用的日记需先遗忘引用才能物理删除**。引用计数可用 `GET /diaries/{id}/references` 查看；只有先 purge 全部引用该日记的记忆（及其证据）与人物，管理员才能永久删除日记。`revoke` 不会解除引用。这是有意的引用完整性保护。
- **备份加密是可选项，且不覆盖旧数据**。默认备份仍为明文；`--encrypt` 需要自行保管口令（丢失不可恢复，无密钥托管/恢复码）；备份没有云同步/跨机复制；恢复要求先停止服务；定时备份/保留策略/恢复演练均需自己配置并定期执行。
- **记忆内容长度受证据上限影响**：默认 `HEARTLOG_MAX_EVIDENCE_CHARS=2000` 同时把单条记忆内容上限收紧为 `min(4000, 该值)`（当前即 2000 字符）。
- **配置为进程启动时加载**，修改 `.env` 后需重启；没有热更新（例外：备份口令由脚本每次运行时从进程环境读取）。

## 后续计划

1. 前端本地 Web UI 已有可运行版本（Dashboard、日记/修订、长期记忆/人物、周月回顾、我的）；后续可补自动化 E2E、无障碍审计与更完整的移动端手势。
2. 真正的认证与多用户隔离（或明确保持单用户并做 OS 级权限加固），以及部署配置在真实 Nginx/Caddy/systemd 环境中的端到端验证。
3. 数据库静态加密与密钥管理（SQLCipher/应用层加密）；备份密钥托管/自动轮换仍是未决问题。
4. 可插拔的本地嵌入模型 / 向量检索（仍保持"每条记忆可回溯到原文证据"）。
5. 导入能力（`import` 来源类型与 `created_by="import"` 枚举已在模型中预留，但当前没有导入 API）。
6. 更完整的"跨备份遗忘"工作流：跨历史备份/导出/审计的保留与清理策略（含 crypto-shredding 类方案）；当前只做到数据库内遗忘 + 手动清理旧备份。
7. 恢复演练的监控告警集成与定期自动执行示例。

---

## 验证状态（最终）

> 本轮新增了遗忘、加密/保留/定时备份、恢复演练、限流与部署安全等能力；以下数量是 Lead 在收尾阶段实际运行 pytest 得到的唯一结果。

**最终自动化测试：**

```bash
PYTHONIOENCODING=utf-8 \
PYTHONPATH='D:\Harness\heartlog\.pyshim;D:\Harness\heartlog\.deps;D:\Harness\heartlog' \
"C:/Users/H/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe" \
  -m pytest tests -q
```

结果：**229 passed, 0 failed, 0 skipped, 1 warning in 29.23s（最近一次运行；多次运行约 28–29s）**（唯一 warning 为既有 Starlette/httpx TestClient 弃用提醒）。

构成：既有 172 个测试（其中含 QA 的 33 个验收/对抗测试）+ 遗忘/派生可见性 9 个 + 应用内限流 30 个 + 备份加密/保留/调度/演练 18 个，合计 **229**。`ruff check app tests scripts` 与 `ruff format --check app tests scripts` 均通过。

**本轮新增能力对应的测试文件（存在性可核对）**：`tests/test_forgetting.py`（遗忘、派生可见性、日记解除阻塞）、`tests/test_backup_productization.py`（加密/保留/调度/恢复/演练）、`tests/test_rate_limit.py`（限流与部署告警）。其他既有测试见 [tests/](tests/)。

**前端验证（收尾阶段实际执行）：**

```bash
cd frontend
npm run typecheck   # 通过
npm run build       # 通过，页面按路由懒加载
npm run preview     # 生产构建本地预览，代理到 127.0.0.1:8000
```

使用本地 Edge + Playwright 手工烟测（不属于仓库内自动化套件）：Dashboard、日记列表/详情/自动保存/修订历史、记忆来源/确认/更正/永久遗忘、人物时间线、周/月回顾、个人资料页、全局搜索、明暗主题、移动端 390px 布局与演示模式。真实后端联调时无控制台错误、桌面与移动端均无横向溢出；后端离线时进入明确标注的演示模式，演示写操作只修改当前会话。未验证真实外部 AI 生成（测试环境 AI 默认关闭）与真实生产反向代理部署。

> 前端自动化测试当前不在仓库内；上面的烟测是交付前手工执行的结果，不应被误解为持续集成覆盖。

**本轮明确未验证或未声称：**

- **真实加密备份的完整灾难恢复未在真实生产环境演练**：加密/解密、保留策略、恢复演练由自动化测试覆盖（临时 SQLite），但"换一台机器、真实磁盘故障后用旧加密备份恢复"未验证。
- **Nginx / Caddy / systemd 参考配置未在真实环境端到端联调**：未实际执行 `nginx -t`、`caddy validate`、`systemd-analyze verify`，也未验证真实 TLS 握手、401/403/429 与日志脱敏；详见 [docs/deployment_security.md](docs/deployment_security.md#1-部署形态)。
- **真实外部 AI provider 的端到端调用未验证**：从未调用外部模型或消耗 API；只覆盖 AI 禁用路径、进程内 fake provider 与 `mock` provider。
- **未做多进程/高并发压力测试**：SQLite 单写者模型与 WAL/`busy_timeout` 已配置，逻辑级并发冲突有测试，但不代表生产吞吐；进程内限流在多 worker 下不共享配额。
- **没有前端 UI**：只有 OpenAPI `/docs` 与 HTTP API；浏览器完整流程未验证。
- **哈希不是不可篡改存储**：可发现意外/静默修改，但不能阻止拥有数据库文件写权限的人删除触发器或直接用备份覆盖。
