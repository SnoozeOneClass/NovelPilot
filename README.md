# NovelPilot

NovelPilot 是一个面向长篇小说生成的本地 Agent Harness。它不把“连续调用模型”当成工作流，而是用确定性的三层领域生命周期管理 Book、Story Arc、Chapter 的规划、评审、审批、版本、Canon 和恢复边界。

当前版本是一次 clean-slate 后端重构：旧文件状态机、thread `RunHost` 和手写 Provider HTTP 层已经退出生产路径。SQLite 是唯一权威状态，Pydantic AI 接管通用模型执行，NovelPilot 自己保留小说领域 Harness、Run Engine 和 Store。

## 核心架构

- 一个 Pydantic AI core，四类有边界的角色：`BookStrategist`、`ArcPlanner`、`ChapterWriter`、`Evaluator`。
- 一个确定性的 Domain Harness：Agent 只能提出或评审内容，不能直接修改权威状态。
- Book、Story Arc、Chapter 各自拥有显式 workspace、review、formal baseline；草稿可原地更新，正式基线不可覆盖。
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

```powershell
npm.cmd run backend:migrate:test
npm.cmd run backend:schema-check
npm.cmd run lint
npm.cmd run typecheck
npm.cmd run test
npm.cmd run frontend:build
npm.cmd run acceptance
npm.cmd run audit:secrets
```

离线测试用 Pydantic AI `FunctionModel` 跑通 full-auto 与 participatory 两条 20 章整书链路，验证的是真实 Agent Executor、Domain Command 和 Store 边界，不调用真实 Provider。

全部离线门禁通过后，才可以显式启动四轮 Grok 4.5 真实观测：

```powershell
npm.cmd run observe:live-book-series -- --case benchmark-mother-natural-book-v1 --profile-id grok-4.5 --runs 4
```

顺序固定为 `full_auto → participatory → full_auto → participatory`。真实结果只记录 completed/failed/not_run、usage、retry、repair 与问题索引，不作为重构成功门槛，也不会自动重跑或现场修复。

## 简历项目表述

**NovelPilot｜本地长篇小说 Agent Harness（个人项目）**

- 针对长篇生成中上下文漂移、状态污染和失败后难恢复的问题，设计 Book／Story Arc／Chapter 三层确定性 Harness，将模型推理与领域状态提交隔离，Agent 只产生候选和评审结论，正式内容通过显式 Command、审批和不可变 baseline 落库。
- 使用 Pydantic AI 重构模型连接与结构化输出底座，按 Provider 协议绑定 opaque model id；实现统一能力校验、30 分钟 activation deadline、最多 6 次真实请求预算及类型化失败证据，避免按具体模型硬编码。
- 基于 SQLAlchemy 2 Core、Alembic 与异步 SQLite 构建 34 表 LT1 生命周期模型、项目内 CAS 内容存储和 Transactional Outbox；实现单写者 Run Engine、幂等命令、CAS、崩溃重放、协作式暂停和专用失败重试。
- 建立 full-auto／participatory 双模式整书离线验收和四轮真实模型非救援观测体系；工程成功由确定性测试门禁判定，概率性模型表现独立记录，便于稳定性分析和后续消融实验。

真实观测完成后，可把“完成章数、自动 retry/repair 次数、token 与零技术救援轮次”补成量化结果；在观测前不把概率性成功写进简历。
