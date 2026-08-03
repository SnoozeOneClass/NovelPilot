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

可从不含真实凭据的 `config/llm-profiles.example.json` 复制字段结构；只把 API key 写入 `.local.json`。

运行时只接受 schema version 2，并要求 capability evidence 与当前配置 fingerprint 一致。项目可为 Book、Arc、Chapter、Evaluator 分别选择 Profile；未指定时使用 default Profile。`model_id` 不参与领域分支，同一 `api_family` 下切换模型不改变 Harness 结构。

目前只支持两个显式协议，协议由 Profile 选择，不能根据模型名推断或静默切换：

- `openai_responses`：`base_url` 必须以 `/v1` 结尾，Adapter 再拼接 `/responses`。未配置 `request_options.max_tokens` 时，请求不会发送 `max_output_tokens`。
- `anthropic_messages`：`base_url` 必须是 `/v1/messages` 之前的服务根地址，不能以 `/v1` 结尾；Adapter 会拼接 `/v1/messages`。该协议必须显式设置一个经真实探测确认的较大 `request_options.max_tokens`，例如 `65536`，不会采用 Pydantic AI 的隐式 `4096`。

`request_options` 属于 Profile fingerprint，不能包含 API key、Authorization、Cookie 或签名 URL。完整响应若以 `length`/`max_tokens` 截断会明确失败并进入失败暂停，不会提交半截 JSON 或正文。

创建或修改 Profile 后，用同一个生产 Adapter 探测结构化输出、文本流、usage，以及需要时的工具调用：

```powershell
npm.cmd run profile:probe -- grok-4.5
npm.cmd run profile:probe -- tool-profile --require-tools
```

所有探测通过后才会原子更新 capability evidence；`--no-write` 只执行探测。任何 `api_family + base_url + model_id + request_options` 变化都会使旧 evidence 失效，旧迁移标记也只显示为 stale，不能替代当前 Adapter 探测。命令不会打印 API key。

运行中的项目若在领取下一任务时发现 Profile 缺失、disabled 或 capability stale，会用零 Provider 请求写入一次明确失败并进入 `failure_paused`，不会让 queued task 被后台反复领取。修好配置并重新探测后，仍需用户显式 Retry。

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
5. Book 基线明确规划有序 Arc 拓扑；full-auto 自动提交通过评审的当前 Arc 契约，participatory 对每个 Arc 显示一个批准动作。Arc 的收束检查点由获批的完整逐章大纲自动推导，不需要用户填写章号。
6. Chapter 自动执行 plan → draft → observe → evaluate → commit；没有章节人工审批。
7. 耗尽 Arc 获批的完整逐章大纲、到达 Harness 派生的 `closure_cumulative_chapter_count` 时，只会触发收束评估。Arc 契约确实被正式 Chapter/Canon 事实满足后，Harness 才提交 formal closure。
8. 非最终 formal closure 由 Harness 按 Book 拓扑确定性产生下一 Arc handoff；只有规划中的最终 Arc closure 才触发 Book completion evaluation。用户给出的章节数只是软规模建议，偏离本身不会创建额外 Arc 或阻止 formal completion。
9. 用户反馈只会先排队，由 Run Engine 在当前原子动作结束后的安全边界按 FIFO 应用。页面会显示反馈是 queued、applied 还是 dismissed。
10. 如果系统显示 creator question，回答会绑定具体 owning layer 和来源 review；系统执行或评估契约错误不会伪装成用户待办。
11. 需要时 Pause。当前模型 activation 会正常收口，系统在下一个安全边界暂停。普通暂停可 Resume；失败暂停只能使用专用 Retry。
12. 全书达到 formal completion 后导出 Markdown。导出只包含正式章节。

页面刷新、SSE 重连、切换项目和普通 GET 不会改变 Route。

## 5. 权威数据、导出和旧输出

```text
data/novelpilot.sqlite3       # 唯一权威应用库
data/backups/                 # 一致快照及 manifest
data/backend-real-acceptance/ # 5.4-mini 工程真实场景报告与隔离数据库
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

备份可以来自当前迁移树中的旧 schema revision，因此应在不兼容迁移前先执行备份命令。Restore 验证 manifest、文件 hash、integrity、FK、备份自身 revision 与 Blob hash，在 staging 库升级到当前 head 后再原子替换整库。它不支持把一个项目合并进另一个正在运行的数据库。

## 7. 后端质量与真实场景门禁

```cmd
npm.cmd run test:fast
npm.cmd run test:backend-real
npm.cmd run acceptance
npm.cmd run architecture:inventory
npm.cmd run audit:secrets
```

其中：

- `test:fast` 在隔离临时库执行 fresh migration、schema drift/health、
  downgrade/upgrade 往返，再执行 backend lint/type check 和无模型单元/契约测试。
  它不会读取或迁移普通项目数据库；它能快速发现局部错误，但不能单独宣称后端可运行；
- `test:backend-real` 固定显式绑定 `jemmy-gpt-5.4-mini`，先通过生产 Adapter
  探测 structured output、正文流和 usage，再在隔离空数据库中运行 S1～S5；
- 工程场景通过 `create_app()`、FastAPI lifespan、唯一 Run Engine、公开 API、
  Pydantic AI、真实 Provider、Domain Command 与 SQLite Store，不提交内部结果或
  手工写领域状态；
- `acceptance` 先运行 fast gate，再运行付费真实场景；失败报告和场景数据库保留在
  `data/backend-real-acceptance/`；
- `architecture:inventory` 只检查实现与局部测试所有权，明确不是验收结论；
- `test:backend-real` 与 `acceptance` 不会改变当前 selected Profile，也不会启动
  `experiment:live-book`；
- 当前后端阶段不把前端 lint/test/build 纳入验收。后端通过四次长跑后再单独优化前端；
- secret audit 扫描 `data/` 和 `output/`，发现 API key 时只报告脱敏路径、
  Profile id 和值类型。

## 8. 四次真实模型观测

只有工程真实场景通过后才由用户手动执行。先启动后端，再用一个独立命令运行：

```powershell
npm.cmd run experiment:live-book
```

runner 默认使用应用当前选中的 Profile；需要刻意覆盖时才传
`-- --profile-id <profile-id>`。当前选中测试 Profile 是
`jemmy-gpt-5.6-terra`：OpenAI Responses 协议、模型 `gpt-5.6-terra`、base URL
`https://api.jemmy.icu/v1`。runner 只检查本地已记录的 Profile readiness、Prompt
SHA-256、固定四轮顺序与 fingerprint，不会先发 Provider 探测请求，也不会输出
secret。四轮各创建一个全新普通项目：

```text
1 full_auto
2 participatory
3 full_auto
4 participatory
```

终端会立即刷新真实阶段播报，不显示无法证明的完成百分比：

```text
Experiment <series-id> started | profile=<profile-id> | schedule=...
[1/4 full_auto] Book | phase=active/drafting | task=book.discuss#1/running | committed=0 | run=running | elapsed=00:00:03
[1/4 full_auto] actor submitted recommended Book input | elapsed=00:04:12
[1/4 full_auto] Chapter 3 | phase=drafting/drafting | task=chapter.draft#2/running | transport_retries=1 | committed=2 | run=running | elapsed=00:18:42
[1/4 full_auto] still running | Chapter 3 | ... | elapsed=00:19:42
```

只有 compact 权威状态发生变化才播报新阶段；连续 60 秒没有变化才输出一次
`still running` 心跳。可用 `-- --heartbeat-seconds <seconds>` 显式调整，
但必须为正数。播报只包含槽位、生命周期、任务/attempt 状态、重试计数、
已提交章节数和耗时，不包含 Prompt、Context、正文、工具内容或 secret。

固定 actor 只执行正常产品动作：推荐 Book 回答、Book 批准，以及 participatory Arc 批准。它没有 Retry、Resume、Pause、数据库编辑、Prompt 编辑或模型输出修改能力。

每轮结束立即写入独立脱敏报告；自然失败不补跑该轮，只要 Provider 仍可调用就继续下一个新项目。鉴权、额度、Profile 或能力问题阻止后续调用时，剩余 slot 标记 `not_run`。aggregate 只汇总事实，不生成 4/4 verdict。

命令会在 `data/live-observations/latest-series.json` 写入最新批次指针，并在批次目录持续更新 `series.json`：`active_observation` 是最新的非权威槽位观察，会被后续观察覆盖；`running` 表示仍在执行或曾被意外中断，`finished` 只表示四个 slot 的证据采集已经收束，不代表四本小说成功。slot 报告落盘后会清除对应 `active_observation`。结束后读取 `aggregate.json`、四份 slot 报告和普通项目数据库，再让 Codex 做一次集中分析。运行期间不需要 Codex 观察，也不会自动诊断、修改代码或补跑。

该命令使用普通本地数据库，不创建隔离数据库，也不隐式重置或迁移数据库。开发期如果决定清空旧测试数据，应在服务停止时作为独立且显式的操作完成。

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
