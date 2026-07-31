# NovelPilot

NovelPilot 是一个面向长篇小说生成的本地 Agent Harness。它不把“连续调用模型”当成工作流，而是用确定性的三层领域生命周期管理 Book、Story Arc、Chapter 的规划、评审、审批、版本、Canon 和恢复边界。

当前版本是一次 clean-slate 后端重构：旧文件状态机、thread `RunHost` 和手写 Provider HTTP 层已经退出生产路径。SQLite 是唯一权威状态，Pydantic AI 接管通用模型执行，NovelPilot 自己保留小说领域 Harness、Run Engine 和 Store。

## 核心架构

- 一个 Pydantic AI core，四类有边界的角色：`BookStrategist`、`ArcPlanner`、`ChapterWriter`、`Evaluator`。
- 一个确定性的 Domain Harness：Agent 只能提出或评审内容，不能直接修改权威状态。
- Book、Story Arc、Chapter 各自拥有显式 workspace、review、formal baseline；草稿可原地更新，正式基线不可覆盖。
- 三层 Loop 分别以 Chapter 原子提交、Arc 契约语义收束、Book boundary handoff/completion 为终止条件；章节数只触发检查，不能直接证明完成。
- 正式权威遵循 `Book > Story Arc > Chapter`；下层只能向直属上层提交证据，不能自动替换父层 baseline 或重写已有下游依赖的历史。
- 全自动与参与模式共享同一生成链路；Book 始终由用户批准，参与模式只额外增加每个 Story Arc 的持久审批门禁。
- FastAPI lifespan 管理唯一 async Run Engine；全应用同一时间最多执行一个小说生成任务。
- SQLAlchemy 2 Core + Alembic + `sqlite+aiosqlite`；大型内容进入项目内去重的 SQLite CAS Blob。
- 任务级 Prompt/Context、最终结果、usage、retry 和错误属于执行证据；逐 token delta 只在内存实时流中存在。
- 普通暂停采用安全边界，失败只能显式 Retry；单次 activation 最多 6 个 Provider 请求、其中最多 5 次 transport retry。

更完整的边界见 [架构说明](docs/architecture.md)，能力—测试对应关系见 [验收追踪](docs/acceptance-traceability.md)。

## 技术栈

- 后端：Python 3.13、FastAPI、Pydantic AI、Pydantic、SQLAlchemy 2 Core、Alembic、aiosqlite。
- 前端：React 19、TypeScript、Vite、TanStack Query。
- 模型连接：由 `api_family` 选择 Pydantic AI Provider/Model；`model_id` 是不参与业务分支的 opaque id。
- 数据：单应用 SQLite、WAL、显式短事务、项目内 Content-Addressed Storage。

## 本地启动

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm.cmd --prefix frontend install
npm.cmd run backend:migrate
```

Profile 只支持 `openai_responses` 与 `anthropic_messages` 两种显式协议。配置后通过同一生产 Adapter 写入能力证据：

```powershell
npm.cmd run profile:probe -- <profile-id>
```

分别启动：

```powershell
npm.cmd run backend:dev
```

```powershell
npm.cmd run frontend:dev
```

打开 `http://127.0.0.1:5173`。详细配置、备份恢复与操作流程见 [本地使用](docs/local-usage.md)。

## 数据与导出

- 权威运行库：`data/novelpilot.sqlite3`。
- 一致备份：`data/backups/`，通过 SQLite Online Backup API 生成并带 hash manifest。
- 本地 Profile/密钥：`config/llm-profiles.local.json`，不进入 SQLite。
- 每本小说唯一对外文件能力是 Markdown 导出；导出只读取已提交 Chapter baseline，不读取草稿或实时流。
- `output/` 中原有旧项目不会被迁移、读取或删除，只保留为人工参考。

项目选择是浏览器工作台状态，不是后端“当前项目”。所有 API 都显式携带 `project_id`。

## 质量门禁

```cmd
npm.cmd run test:fast
npm.cmd run test:backend-real
npm.cmd run acceptance
npm.cmd run architecture:inventory
npm.cmd run audit:secrets
```

快速测试继续精确覆盖 Schema、SQL 约束、协议错误分类和局部生命周期；其中的
`FunctionModel`、Mock、手工 seed 只属于单元或 `synthetic_integration` 证据，
不能证明后端生产路径可运行。

工程验收固定使用 `jemmy-gpt-5.4-mini`，从隔离空数据库启动生产
`create_app()`/lifespan/Run Engine，通过公开 API 和真实 Provider 运行 S0～S5：
基础 Book→Arc→Chapter 交接、开放世界证据、正文与派生证据权威、Chapter→Arc→Book
语义压力，以及关闭/重开后的持久恢复。`acceptance` 会先运行无模型 fast gate，
再运行这些付费场景；报告落在 `data/backend-real-acceptance/`。

`architecture:inventory` 只是非结论性的静态所有权清单。当前后端阶段不把前端优化
纳入验收，待四次长跑通过后再集中处理前端。

工程真实场景通过后，才由用户显式启动当前冻结的四轮真实模型观测：

```cmd
npm.cmd run experiment:live-book
```

命令使用应用当前选中的 Profile，顺序固定为
`full_auto → participatory → full_auto → participatory`。工程验收不会调用这条命令，
不会替换当前 selected Profile，也不会重置普通项目数据库。终端只播报权威阶段变化、
正常 actor 动作和每 60 秒无变化心跳；命令结束后通过
`data/live-observations/latest-series.json` 定位证据，再由后续 Codex 会话分析。

## 简历项目表述

**NovelPilot｜本地长篇小说 Agent Harness（个人项目）**

- 针对长篇生成中上下文漂移、状态污染和失败后难恢复的问题，设计 Book／Story Arc／Chapter 三层确定性 Harness，将模型推理与领域状态提交隔离，Agent 只产生候选和评审结论，正式内容通过显式 Command、审批和不可变 baseline 落库。
- 使用 Pydantic AI 重构模型连接与结构化输出底座，按 Provider 协议绑定 opaque model id；实现统一能力校验、30 分钟 activation deadline、最多 6 次真实请求预算及类型化失败证据，避免按具体模型硬编码。
- 基于 SQLAlchemy 2 Core、Alembic 与异步 SQLite 构建 39 表 LT1 生命周期与分层权威模型、项目内 CAS 内容存储和 Transactional Outbox；实现单写者 Run Engine、幂等命令、崩溃重放、协作式暂停和专用失败重试。
- 建立三层测试证据体系：无模型快速契约、固定低成本模型的生产路径语义压力场景、
  以及四轮约 20 章无技术救援长跑；将跨 Loop 交接、分层权威和崩溃恢复纳入可追踪验收，
  同时用长跑发现未知累积问题。

真实观测完成后，可把“完成章数、自动 retry/repair 次数、token 与零技术救援轮次”补成量化结果；在观测前不把概率性成功写进简历。
