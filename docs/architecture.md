# NovelPilot 架构说明

## 1. 目标与边界

NovelPilot 是本地、单用户、单进程的长篇小说 Agent Harness。同一时间只运行一个小说生成任务，但可以有多个项目停在审批、普通暂停或失败暂停状态。

系统解决的不是“让模型写一段文本”，而是四类稳定性问题：

1. 模型连接、流式读取、结构化输出和错误分类不再由业务代码手搓；
2. Agent 输出不能直接污染小说事实；
3. Book、Story Arc、Chapter 的审批与版本边界必须可恢复、可审计；
4. 浏览器刷新、SSE 断线和进程重启不能重复驱动生成。

不在当前版本实现的能力包括账号、多用户、跨进程消息、云部署和 Experiment Lab UI。未来实验母本会引用同一套正式 baseline/CAS/evidence，不会引入另一档项目或日志模型。

## 2. 总体分层

```text
React 工作台
  ├─ 显式 HTTP Command：创建、Start、Pause、Resume、Retry、审批、反馈、导出
  └─ Read/SSE：权威投影、durable cursor、可丢失 live delta
                         │
FastAPI lifespan         ▼
  ├─ 唯一 async Run Engine
  ├─ Pydantic AI Agent Executor
  ├─ Domain Harness / Route / Commands
  └─ SQLAlchemy Core Repositories
                         │
                         ▼
               SQLite + project-owned CAS
```

依赖方向固定为：

```text
API -> application commands / queries
Run Engine -> Route + Agent Executor + Domain Commands
Agent Executor -> Pydantic AI + execution evidence
Domain Commands -> layer-specific repositories
Repositories -> SQLAlchemy Core / AsyncConnection
Domain -X-> FastAPI, Pydantic AI, SQLAlchemy Row, live stream
```

旧 `RunHost`、旧 `app.llm` HTTP gateway、JSON/JSONL repository 和 active-project 文件均已删除。生产代码只有这一条路径。

## 3. Pydantic AI 与领域 Harness 的边界

Pydantic AI 接管通用能力：Provider/Model 调用、原生 JSON Schema、文本流、usage、SDK 异常与取消传播。NovelPilot 保留业务能力：

- 有限 task registry 与冻结 Task Plan；
- Prompt/Context 如何编排；
- Book、Arc、Chapter Route；
- 审批、revision、Canon、completion；
- Run 控制、幂等、恢复和 Store。

四类角色均无领域写 Tool：

| 角色 | 职责 |
| --- | --- |
| BookStrategist | Book 讨论、综合、修订、进度/完成判断 |
| ArcPlanner | 当前 Story Arc 的滚动规划与修订 |
| ChapterWriter | Chapter 计划、正文、观察与局部修订 |
| Evaluator | 按 Book/Arc/Chapter rubric 只读评审 |

计划、观察、评估使用原生结构化输出；章节正文使用文本流。Profile 不满足所需能力时在零 Provider 请求处失败，不静默降级。`api_family` 决定协议适配，`model_id` 只作为 opaque id；从 Grok 换到同协议 GPT 不改变 Agent、Route 或领域类型。

## 4. LT1 生命周期与正式基线

Book、Story Arc、Chapter 分别拥有显式生命周期表，不使用 `scope_kind` 万能表。

```text
mutable workspace
  -> review submission（冻结候选）
  -> evaluator review
  -> approval/policy authorization
  -> immutable formal baseline
```

- 未通过审阅的工作稿可以原地更新，不为每次编辑创建 revision。
- 已通过并提交的正式 baseline 不可覆盖；后续修改派生新 workspace。
- 章节内部影响由 Chapter 层处理；影响 Story Arc 或全书时显式升级 change request。
- Agent 只提出、修订或评审；Harness 通过 Domain Command 写权威状态。
- 上游 baseline 更新后，下游 workspace 会标记 stale，并通过显式 rebase command 绑定最新依赖后重新生成。

产品门禁：

- Book：两种模式都必须完成独立评审和用户显式批准。
- Story Arc：full-auto 由 policy command 提交；participatory 每个 Arc 形成一个持久审批门禁。门禁形成后切回 full-auto 也不能绕过。
- Chapter：独立评审通过后自动提交，没有人工章节审批。

## 5. SQLite、CAS 与 Transactional Outbox

SQLite 是唯一权威状态。数据库包含 34 张应用表，由 Alembic initial revision 和共享 `MetaData` 共同约束。

大型 Prompt、Context、typed result、正文和诊断附件存入项目拥有的 Content-Addressed Storage：

- Blob 身份是规范化未压缩字节的 SHA-256；
- 仅同一项目内按 hash 去重；
- 不跨项目共享 Blob，不需要 refcount 或后台 GC；
- 项目删除通过外键级联删除该项目的内容、证据和领域行；
- Fixture 将来作为独立不可变资产发布，不和普通项目 Blob 共生命周期。

`domain_events` 同时承担 Transactional Outbox：领域状态、command receipt 与事件在同一个 SQLite 事务提交。事务成功后，SSE 才能按 sequence/cursor 读取事件；不存在“状态已改但通知丢失”或“事件已发但状态回滚”的中间状态。这里不需要 Redis，因为系统没有跨进程消费者。

Route、恢复、唯一约束和完成判断只读关系 metadata，不解压 Blob，也不从历史事件或 token 流重新推导权威状态。

## 6. Run Engine、事务与恢复

FastAPI lifespan 创建并关闭唯一 `AsyncEngine`、Run Engine 和内存 live fan-out。Run Engine 每一步遵守：

```text
短事务 claim
  -> 事务外 Provider/确定性计算
  -> 短事务写 terminal evidence
  -> 独立 Domain Command 事务
  -> 重新读取 Route
```

模型请求、流式等待、SSE backpressure 和用户审批等待都不持有数据库事务。

- SQLite `engine_slot` 强制全局最多一个实际执行任务。
- Pause 写入 desired state，当前 activation 正常收口后在安全边界暂停。
- 普通 Resume 只适用于普通暂停；`failure_paused` 只能通过专用 Retry 创建新 attempt。
- running attempt 使用 lease/heartbeat。启动 reconcile 对过期 attempt 最多自动创建一次 `crash_replay`；再次中断则失败暂停。
- 已有完整 result 但尚未 delivery 时只补 Domain Command，不重复调用模型。
- 同一 Domain Command 的 idempotency key 与 request fingerprint 保证重放不重复提交 baseline 或事件。

## 7. 重试、超时与证据

每个 task activation 是一次全新的无隐藏记忆 Agent run：

- 最多 6 个真实 Provider 请求；
- 其中最多 5 次 transport retry；
- structured output 最多额外一次 model repair，但与 transport retry 共用六次总预算；
- T1：connect/pool 10 秒、write 60 秒、read 10 分钟、activation 30 分钟。

连接错误、408/409/425/429/5xx 等按合同分类；鉴权、配置、能力和明确 invalid request 快速失败。失败 task 持久化类型化错误与已脱敏诊断，不在后台无限等待 Provider。

长期证据以完整任务为粒度：冻结的 Task Plan、输入 manifest、消息、完整工具过程、最终结果、usage、retry 链和错误附件。逐 token delta 不进入 SQLite，只发布到进程内 `LossyLiveFanout`；刷新和重启不回放旧 delta。

## 8. API 与前端状态模型

所有项目 endpoint 显式使用 `project_id`。前端 localStorage 只记当前打开哪个项目，不是后端领域事实。

- Query 直接返回 current baseline/workspace、pending gate、blocking failure 和可执行 commands。
- Mutation 必须携带 `Idempotency-Key`，返回最新权威投影。
- SSE 先按 durable event cursor 补进度，再附加可丢失 live prose。
- React effect、页面刷新、项目切换和 SSE 重连都不能 Start、Resume 或 Retry。
- 诊断 endpoint 只返回 task/attempt/profile fingerprint/usage/error metadata，不返回或解析正文 Blob。

## 9. 备份、恢复与导出

继续运行所需的备份是整个应用数据库的一致快照，不是复制 `.sqlite3` 主文件：

1. 服务停止或所有运行到达安全边界；
2. SQLite Online Backup API 创建快照；
3. 校验 integrity、foreign keys、schema revision 和每个 Blob hash；
4. 写入绑定文件大小、SHA-256、event sequence 和 Blob count 的 manifest。

Restore 要求 FastAPI 已停止且无 WAL/SHM sidecar，验证后原子替换整库，不做项目行级 merge。

单本小说只提供 Markdown 导出。导出按 book ordinal 读取正式 Chapter baseline，计算 snapshot fingerprint 与 content hash；workspace 草稿、失败 attempt 和 live delta 永远不会进入正文。

## 10. 实验与真实观测

未来母本冻结会在明确节点把正式 baseline、Canon、任务证据 identity 和 manifest 发布为不可变 bundle。普通项目与母本在冻结前使用完全相同的领域模型和持久化规则。

首轮真实观测是独立的 post-acceptance series：同一 Prompt/hash、Profile/fingerprint、代码 source hash、Harness contract 和 actor policy，按 `full_auto → participatory → full_auto → participatory` 创建四个新项目。合法 Book/Arc 产品动作不算技术救援；runner 不调用 Retry/Resume，不修改模型输出、Prompt、Profile 或数据库。结果只形成事实报告和问题索引，不反向决定离线工程验收。
