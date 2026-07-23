# NovelPilot 本地使用

## 1. 安装

在仓库根目录：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm.cmd --prefix frontend install
```

根脚本通过 `scripts\python.cmd` 选择 `.venv\Scripts\python.exe`、Conda `.venv\python.exe` 或当前 PATH 中的 Python。

## 2. 数据库初始化与启动

```powershell
npm.cmd run backend:migrate
```

分别启动后端和前端：

```powershell
npm.cmd run backend:dev
```

```powershell
npm.cmd run frontend:dev
```

访问 `http://127.0.0.1:5173`。后端默认位于 `http://127.0.0.1:8010`。

FastAPI 启动时会校验 schema revision、integrity、foreign keys 与 Blob hash，并由 lifespan 启动唯一 Run Engine。不要启动多个 Uvicorn worker。

## 3. LLM Profile

Profile 与 API key 位于 git ignored 的：

```text
config/llm-profiles.local.json
```

运行时只接受 schema version 2，并要求 capability evidence 与当前配置 fingerprint 一致。项目可为 Book、Arc、Chapter、Evaluator 分别选择 Profile；未指定时使用 default Profile。`model_id` 不参与领域分支，同一 `api_family` 下切换模型不改变 Harness 结构。

从旧本地配置一次性迁移：

```powershell
scripts\python.cmd scripts/migrate_profile_config.py
```

迁移不会打印或移动 API key。能力不满足 `text_streaming` 或 `native_json_schema` 时任务明确失败，不会降级到另一种输出协议。

## 4. 正常创作流程

1. 在工作台创建项目，选择 `full_auto` 或 `participatory` 和 capability-ready Profile。
2. 点击 Start。浏览器仅发出一次显式命令；关闭页面不停止后端流程。
3. BookStrategist 基于初始 Prompt 逐次提出一个高价值问题。可以选择推荐回答，也可以自由输入。
4. Book 候选通过 Evaluator 后仍会等待显式批准；两种模式都不能跳过。
5. full-auto 自动提交通过评审的 Arc；participatory 对每个 Arc 显示一个批准动作，可以采用建议章节数。
6. Chapter 自动执行 plan → draft → observe → evaluate → commit；没有章节人工审批。
7. 需要时 Pause。当前模型 activation 会正常收口，系统在下一个安全边界暂停。
8. 普通暂停可 Resume；失败暂停只能使用专用 Retry。Retry 创建新 attempt，不改写原证据。
9. 全书达到 completion 后导出 Markdown。导出只包含正式章节。

页面刷新、SSE 重连、切换项目和普通 GET 不会改变 Route。

## 5. 权威数据、导出和旧输出

```text
data/novelpilot.sqlite3       # 唯一权威应用库
data/backups/                 # 一致快照及 manifest
data/live-observations/       # 四轮真实观测报告
config/*.local.json           # Profile 与本地密钥
output/                       # Markdown 导出及保留的旧输出
```

旧 `output/project-*` 文件项目不会自动迁移，也不会被新后端读取或删除。新小说的状态恢复依赖 SQLite current rows、pending gates、attempt/delivery metadata，不依赖旧 JSONL 或实时 token。

## 6. 备份与恢复

备份前停止后端，或保证没有 running/pause_requested Run、running attempt 和已占用 engine slot：

```powershell
npm.cmd run backend:backup -- --destination data\backups\novelpilot-2026-07-23.sqlite3
```

校验备份：

```powershell
npm.cmd run backend:backup:validate -- data\backups\novelpilot-2026-07-23.sqlite3
```

恢复前必须停止 FastAPI；目标库旁不能遗留 `-wal` 或 `-shm`：

```powershell
npm.cmd run backend:restore -- data\backups\novelpilot-2026-07-23.sqlite3
```

Restore 验证 manifest、文件 hash、integrity、FK、schema 与 Blob hash 后原子替换整库。它不支持把一个项目合并进另一个正在运行的数据库。

## 7. 离线质量门禁

```powershell
npm.cmd run backend:migrate:test
npm.cmd run backend:schema-check
npm.cmd run backend:lint
npm.cmd run backend:typecheck
npm.cmd run backend:test
npm.cmd run frontend:lint
npm.cmd run frontend:typecheck
npm.cmd run frontend:test
npm.cmd run frontend:build
npm.cmd run acceptance
npm.cmd run audit:secrets
```

其中：

- migration gate 执行 fresh upgrade、schema check、空库 downgrade/upgrade；
- backend tests 覆盖数据库负约束、三层生命周期、Pydantic AI、Run Engine、恢复、API 和双模式 20 章整书；
- acceptance 把产品能力映射到实现和离线测试，并检查旧运行路径已消失；
- secret audit 扫描 `data/` 和 `output/` 下数据库、备份、导出与报告，发现 API key 时只报告脱敏路径、Profile id 和值类型。

## 8. 四次真实 Grok 4.5 观测

只有上一节全部通过后才执行。先启动后端，再运行：

```powershell
npm.cmd run observe:live-book-series -- --case benchmark-mother-natural-book-v1 --profile-id grok-4.5 --runs 4
```

runner 会先验证 Prompt SHA-256、固定四轮顺序、Profile capability 与 fingerprint，不会输出 secret。四轮各创建一个全新普通项目：

```text
1 full_auto
2 participatory
3 full_auto
4 participatory
```

固定 actor 只执行正常产品动作：推荐 Book 回答、Book 批准，以及 participatory Arc 批准。它没有 Retry、Resume、Pause、数据库编辑、Prompt 编辑或模型输出修改能力。

每轮结束立即写入独立脱敏报告；自然失败不补跑该轮，只要 Provider 仍可调用就继续下一个新项目。鉴权、额度、Profile 或能力问题阻止后续调用时，剩余 slot 标记 `not_run`。aggregate 只汇总事实，不生成 4/4 verdict。series 完成后停止，不自动诊断或修复，等待后续分析。

## 9. 应留在本地的内容

以下均已 git ignored，不应强制加入版本库：

```text
config/*.local.json
data/
output/
.venv/
node_modules/
frontend/dist/
```
