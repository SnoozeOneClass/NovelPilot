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
| BookStrategist | Book 讨论、候选综合与正式修订候选 |
| ArcPlanner | 当前 Story Arc 契约、从生效点到收束检查点的完整逐章大纲，以及获得授权后的未来计划修订候选 |
| ChapterWriter | 在 Harness 分配的当前 Arc 大纲项内完成 Chapter 细化计划、正文、观察与局部修订 |
| Evaluator | 按任务绑定的 Book/Arc/Chapter、父层审查、Arc 收束、Book 边界和证据纠正策略只读评审 |

计划、观察、评估使用原生结构化输出；章节正文使用文本流。结果格式与线协议相互独立：结构化任务由 Agent 保留一次原生输出修复，但每次底层 Provider 调用同样使用流式连接，完整 Pydantic 校验通过前不会形成结果或领域事实。

Profile 不满足所需能力时在零 Provider 请求处持久化一次 failed attempt，随后 Run 进入 `failure_paused`，不会让 queued task 在 Run Engine 中形成热循环，也不会静默降级。`api_family` 只允许 `openai_responses` 与 `anthropic_messages`，并决定对应的 Pydantic AI Model/Provider 和官方 SDK；`model_id` 只作为 opaque id，从 Grok 换到同协议 GPT 不改变 Agent、Route 或领域类型。Responses 的 base URL 以 `/v1` 结尾，Messages 的 base URL 不包含末尾 `/v1`，避免 SDK 拼接出重复路径。

Profile capability evidence 绑定完整的 `api_family + base_url + model_id + request_options`，并通过同一生产 Adapter 真实探测 structured output、text stream、usage 和声明需要的工具调用。配置变化后旧 evidence 自动失效。

## 4. LT1 生命周期与正式基线

Book、Story Arc、Chapter 分别拥有显式生命周期表，不使用 `scope_kind` 万能表。

```text
mutable workspace
  -> review submission（冻结候选）
  -> evaluator review
  -> approval/policy authorization
  -> immutable formal baseline
```

每个三层 Workspace 还拥有一个显式 `work_cycle_id`。`lock_version` 只解决一次
物理写入的 CAS，`work_cycle_id` 则标识一次完整的语义工作周期；候选评审和
Agent Task 必须冻结同一个周期。`local_repair` 不是“取最新评审后继续”的状态，
而是一条由 `active_repair_review_id` 指向的单次授权边。Repair Task 同时绑定
该周期和对应层的 candidate review ID，Route、重启恢复与 Domain Command 都按
这组精确身份判断，禁止通过时间戳、相同 Blob、相同 baseline 或 current 指针
猜测授权。应用反馈、父层 rebase 或打开新的 successor workspace 都会创建新
周期并清除旧 repair 授权。

同一工作周期最多自动进行一次候选语义纠正；重新评审仍要求同类修复时进入明确
失败/停滞路径，不再自动循环。这与连接层的五次 transport retry 完全不同：
Provider 重放始终执行同一份冻结 Task Plan，不能产生新的领域纠正授权。

Book/Arc 的这一次纠正按“完整同层语义问题”授权，而不是按 Evaluator 首次点名的
字段授权。`observed_components` 只记录诊断位置；Harness 为 Repair 冻结完整同层
候选包络，Agent 扫描每个原始 issue 在包络中的全部出现位置并只返回实际变化。
Book 包络不含正式标题且 topology 只允许未来 suffix，Arc 包络只含当前 Arc
candidate；Verifier 读取 before/after、原始 issue ledger 和 Harness 生成的实际
变化清单。Chapter 继续使用现有 dependency-aware 精确组件授权，不为形式对称而
扩大权限。纠正仍只有一轮，正式 baseline 和历史 prefix 始终不可写。

多 Arc 链路同样使用精确来源：Arc 2 及以后只能消费前一正式 Arc closure 产生的
当前 `BookProgressHandoff`；Book completion 评审和由其打开的 Book successor
workspace 都保留实际使用的 `source_book_progress_handoff_id`。最终 Arc 的完成
评审不会因为它自身不再产生“下一 Arc handoff”，就丢失进入该 Arc 时使用的前序
handoff。

- 未通过审阅的工作稿可以原地更新，不为每次编辑创建 revision。
- 已通过并提交的正式 baseline 不可覆盖；后续修改派生新 workspace。
- 章节内部影响由 Chapter 层处理；影响 Story Arc 或全书时只能逐级升级 change request。
- Agent 只提出、修订或评审；Harness 通过 Domain Command 写权威状态。
- 上游 baseline 更新后，未提交的下游 workspace 会失效并显式重绑；已提交历史不会被自动 rebase/replay。
- 每个正式 Book baseline 冻结一个有序语义 Arc 拓扑，并且恰好标记一个最终 Arc。Book 只规定各 Arc 在全书中的职责、核心目标、前序交接和退出条件，不分配章节标题、章节事件、场景或每弧章数。
- 每个 Book Arc contract 通过 `completion_requirement_keys` 显式承担当前 Book completion contract 中的要求。Harness 校验 key 身份、完整覆盖和 successor future suffix 的责任归属；现有 Book Evaluator 在同一次评审中逐项判断 `aligned / strength_mismatch / infeasible`，不新增第二个评审调用。这里的 `aligned` 是语义蕴含：负责 Arc 的目标与退出条件达成后，要求中不可省略的命名主体、行为、排除项、因果边、结果强度和证据预期必须必然成立；仅仅“方向相关”“澄清角色”或“排除统一布局”不能替代精确责任，缺失或只被暗示时必须判为 `strength_mismatch`，但不要求逐字匹配。Arc 只获得分配给自己的要求并负责把它们落实为 closure signals 与逐章大纲，因此 Book 仍不越级规划 Chapter。
- 用户给出的章节数只作为软规模建议传给规划 Agent，不参与 Route、Arc 收束、Book 完成或拓扑修订门禁。
- 每个正式 Arc baseline 冻结一个明确生效点和恰好覆盖其未来区间的 `chapter_outline`；初始 Arc 必须至少包含一章，合法 successor 可以在生效点已经等于新检查点时包含零个未来项。
- Harness 创建 Chapter 时确定性分配唯一大纲项，并把来源 Arc baseline 固定到 Chapter 身份上。Chapter 自身的 revision 不能改写这项 provenance。
- Arc successor 只替换生效点之后的未来大纲。已有正式 Chapter 保持原来源；唯一尚未提交的当前 Chapter 在同一事务中重绑到 successor 并清空依赖旧大纲的草稿内容。
- 后端把同一 `story_arcs.id` 的 baseline lineage 投影成一条连续 Arc：已存在 Chapter 使用稳定 provenance，未来项只取当前 baseline。前端和模型不得自行拼接 v1/v2。

产品门禁：

- Book：两种模式都必须完成独立评审和用户显式批准。
- Story Arc：full-auto 由 policy command 提交；participatory 每个 Arc 形成一个持久审批门禁。门禁形成后切回 full-auto 也不能绕过。
- Chapter：独立评审通过后自动提交，没有人工章节审批。

### 4.1 三层终止契约与层级权威

`Book > Story Arc > Chapter` 是正式语义权威顺序。下层可以提交证据和直属上层审查请求，但不能判断或替换上层 baseline：

- Chapter 只在当前 Book/Arc baseline、当前 Canon 和当前章目标齐备时启动。正文、observations 与 Canon intent 通过独立评审并由原子 Command 提交，才算 Chapter 退出成功。
- Arc 只在当前 Book baseline、当前 ordinal 对应的精确 Book Arc contract，以及合法的上一 Arc progress handoff（首 Arc 除外）齐备时启动。`closure_cumulative_chapter_count` 由 Arc 获批时的生效点加完整大纲长度确定，只触发最低限度的收束检查；只有 Arc 契约被已提交事实满足并形成 formal closure，Arc 才算结束。
- 非最终 formal Arc closure 不调用 Book 模型任务；Harness 只按当前 Book 拓扑确定性提交下一 ordinal 的 handoff。只有规划中的最终 Arc closure 才触发 `evaluate.book_completion`，并使用所有正式 Arc closure 与累计 Canon 判断整书终止。它不能隐式创建未规划的 Arc。

Arc Planner 和 Arc candidate/closure Evaluator 可以看到当前 Arc 的完整逐章大纲。正常 Chapter 计划与正文只看到当前项和至多一个下一项；Chapter 观察、Canon 抽取和评审只看到当前项。大纲遵循软语义约束：Chapter 必须完成宏观职责，但不要求标题、措辞、场景数量或事件描述逐字匹配。逐章大纲耗尽只触发 Arc 收束检查，不等于收束通过，也不引入周期性 Arc 质量复盘。

Arc 收束或父层审查的自动向下纠正，在同一冻结评审 lineage 中最多一轮。第二次出现同类问题时，若拥有该问题的 Agent 给出了用户可以回答的具体问题，则进入显式 creator wait；执行、评估契约或上下文问题进入失败暂停，不能伪装成等待用户。

首版只允许叙事性 Chapter successor 修改当前 lineage 顶端。只要已有后续 Chapter、formal Arc closure 或 Book handoff，就进入 `waiting_for_user/historical_rewrite_unsupported` 并保持所有权威指针不变。正文逐字不变的 evidence-only correction 可以在 Arc 收束前修复 observations/Canon，但必须证明正文证据成立且后续 Chapter 不冲突。

### 4.2 语义权威、事实证据与逐任务上下文

三层共享同一套语义生命周期，但继续使用各自的物理表和 Domain
Repository。工作稿可以原地修改；Frozen Candidate、Review、Agent
task/attempt、Domain Command/receipt 和 Formal Outcome 完整留证；正式
baseline 只能由同层合法 successor 取代，不能被 Evaluator 或下层静默覆盖。

正式 Chapter prose 是“实际写了什么”的叙事来源。Chapter observation
只输出：

- `summary`：导航摘要，不单独构成事实证据；
- `established_facts[{statement,evidence_hint}]`：模型提取的普通语义事实；
- `canon_proposals`：供后续生成使用的当前状态建议。

Agent 不返回存储 ID、hash、offset 或 locator。Chapter 正式提交时，
Harness 把每条 fact 绑定到具体 Chapter、Chapter baseline、prose ref/hash
和 fact ordinal，并让正式 `observations_ref_id` 指向该 committed
document；接受的 Canon entry 同样绑定具体 Chapter baseline 和 prose。
Observation/Canon 都是可纠正的派生状态，发生争议时回到对应正式 prose，
不能让错误派生信息反向要求修改正确正文。

每个 Agent task 使用固定 CXT1 Context View。模型可见上下文块只暴露六个
属性：`role / scope / time / use / access / target`；内部 ID、hash 和完整
source binding 只写入 `novelpilot-task-context-manifest-v5`。评审或修复任务
恰好拥有一个逻辑 `target_descriptor`；正式契约、正式结果、正式正文、派生
证据、Canon 和旧评审均为只读。Book 不重读全部 Chapter，Arc 只消费当前
Arc 的正式事实，Chapter 只消费当前 assignment、至多下一 assignment、
必要衔接和当前 Canon；系统不引入 RAG 或全历史检索。

Book 讨论上下文也按用途拆开：`book.discuss` 可以读取当前 discussion state
和 transcript，`book.synthesize` 只读取已归并的当前 state；Book
repair/evaluate 不读取 transcript。Book 的 completion contract 与完整 Arc
topology 只进入 Book revision、Book parent review 和 Book completion 等
Book 权威任务；Arc 只接收 Book 明确分配给当前 Arc 的 contract，Chapter
再接收该 Arc 的正式 contract 与当前 outline window，不能把整书
completion/topology 当成本层可评或可改目标。Arc/Chapter candidate
Evaluator 同时读取当前 Arc 已提交 facts，避免在缺少正式历史时做冲突判断。

Book successor Context 明确区分 predecessor、Harness 冻结的历史 Arc 前缀和
当前 candidate：模型看到 `candidate_kind`、历史前缀数量以及从当前候选派生的
拓扑数量/最终 Arc ordinal，不再把 predecessor 的总数或最终 ordinal 暴露成
候选必须保持不变的硬约束。历史前缀相等与 future suffix 权限仍由 Domain
composition/CAS 确定性保护，不交给 Evaluator 重新裁决。

Context policy 不只声明允许组，也声明每个 task kind 的必需组，以及 revision/
evidence correction 所需的至少一个明确授权来源；缺块在 Provider 调用前以
`context_assembly_invalid` 失败。正式 Canon 始终保持 `time=current`，不会因
repair/verify 而误标成候选；只有实际候选块标记为 `pre_repair/post_repair`。
Raw user feedback 只有在仍是 applied feedback 的当前 content ref 时才标记为
`creator_guidance`，父层 review 必须保持 `review_finding /
repair_authorization` 身份。

Evaluator 只允许六类 EP1 blocker：
`explicit_conflict`、`contract_unfulfilled`、
`unsupported_strong_conclusion`、`derived_evidence_mismatch`、
`parent_authority_concern`、`creator_owned_unknown`。普通叙事事实采用开放
世界判断：前文沉默不等于否定，当前 Chapter 可以首次建立普通事实；只有
候选陈述与正式来源存在明确相反陈述时才构成冲突。文学质量、风格、节奏和
soft advisory 偏离不形成 blocker，不触发 Repair 或改变 Route。
EP1 `kind` 决定分类及其最低必需证据，其他普通字段只保留诊断信息，不构成第二套
Route 协议。例如 `contract_unfulfilled` 可以同时携带具体 `support_gap`，冲突之外的
issue 也可以保留完整的候选/正式陈述对；Harness 不因相关诊断冗余而拒绝结果，也不
从这些可选字段猜 Route。只有 `creator_question` 继续专属于
`creator_owned_unknown`，因为它会授权真正的用户等待。
所有能够改变 Route 的字段必须与对应 EP1 issue 原子一致：父层审查必须携带
`parent_authority_concern`，派生证据纠正必须携带
`derived_evidence_mismatch`；字段与 issue 不一致时作为评审契约缺陷暂停，
不能按 Harness 内部优先级猜测模型意图。

Evidence-only correction 只允许在纠正目标仍是当前 Canon entry 的实际来源
Chapter baseline 时，更新语义相同 entry 的 evidence/provenance。后续 Chapter
再次提及同一状态不会夺走原来源；纠正也不会把正确正文或后续正式历史改写成
新的叙事版本。

用户反馈采用 G1 一次性消费：原始反馈不可变留证，在安全边界成为目标层
Workspace 的当前 guidance；下一次 Frozen Candidate/Review manifest 绑定
实际 `guidance_ref_id` 和 `source_feedback_id`；正式提交成功时原子清空
Workspace guidance。已消费的历史反馈不会作为独立 prompt 片段永久重复
注入；需要长期生效的偏好由同层 baseline 保存为显式 advisory projection。
普通 guidance 可以只绑定 `source_feedback_id`，不因此成为纠正 lineage。
只有用户回答正式纠正等待时，`user_initiated` lineage 才必须与该反馈 ID
原子绑定；`review_initiated` lineage 则禁止混入用户反馈。即使两条反馈文本
完全相同，它们仍创建不同的 feedback/work-cycle 身份，旧任务不得被新周期复用。

所有 Formal Outcome 都从自身记录解析历史含义，而不是依赖后来变化的
`current_*` 指针。Book/Arc/Chapter baseline、Arc closure 和 Book
completion 均保存其实际使用的具体上层 baseline、Canon、终端 Chapter、
review/approval 和内容 manifest 绑定。

## 5. SQLite、CAS 与 Transactional Outbox

SQLite 是唯一权威状态。数据库包含 39 张应用表，由 Alembic revision 与共享 `MetaData` 共同约束。

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
- OpenAI 与 Anthropic SDK 内建 retry 均关闭，每个物理请求都进入同一个计数器和证据链；
- commit 前只重放同一个冻结 Task Plan，不从半截 JSON/正文构造 continuation；commit 后的 Domain delivery 不再调用 Provider；
- T1：connect/pool 10 秒、write 60 秒、read 10 分钟、activation 30 分钟。

连接、首事件超时、流空闲超时/中断、408/409/425/429/5xx 等按合同分类并指数退避；鉴权、额度、配置、能力、明确 invalid request、取消和输出截断快速失败。完整响应的 `length`/`max_tokens` 终止不会自动增大上限重跑。失败 task 持久化类型化错误与已脱敏诊断，Run 进入 `failure_paused` 等待显式 Retry，不会重新回到 pending。

HTTP 200/`complete` 也不等于任务已经产生结果。若捕获的真实
`ModelResponse` 只有 thinking 或空白、没有可消费的正文、工具、refusal、文件/
图片或原生结构化输出材料，Executor 将其规范化为 `provider_empty_output`。
检查位于 Pydantic AI `run_stream` 外层，因此 context manager 入场前抛错也能被
识别；系统使用同一 Frozen Task Plan 和同一六请求预算有限重放。连续六次为空时
以 `provider_empty_output_retries_exhausted` 明确失败暂停，不进入语义 Repair 或
pending 循环。

NovelPilot 不按任务或领域层设置产品级输出 token 预算。Responses 在未配置时省略 `max_output_tokens`；Messages 因协议强制要求而使用冻结 Profile 中显式、经探测的大 `max_tokens`，不使用隐式 4096。

长期证据以完整任务为粒度：冻结的 Task Plan、输入 manifest、消息、完整工具过程、最终结果、usage、retry 链和错误附件。每个物理请求记录协议、序号、起止时间、首/末事件时间、HTTP 状态、脱敏 request ID、错误、重试决定和延迟。逐 token delta 不进入 SQLite，只发布到进程内 `LossyLiveFanout`；刷新和重启不回放旧 delta，重放时前端以 `attempt_restarting` 清除被放弃的局部正文。

## 8. API 与前端状态模型

所有项目 endpoint 显式使用 `project_id`。前端 localStorage 只记当前打开哪个项目，不是后端领域事实。

- Query 直接返回 current baseline/workspace、pending gate、blocking failure 和可执行 commands。
- Mutation 必须携带 `Idempotency-Key`，返回最新权威投影。
- 用户反馈 endpoint 只按 FIFO 入队；唯一 Run Engine 在当前原子动作结束后的安全边界应用。失败暂停期间反馈继续保留，不会隐式 Retry。
- creator wait 投影包含拥有问题的层级、来源 review 和具体问题。回答沿同一来源 lineage 入队；普通建议与正式问题回答不会混成同一种状态。
- 最近反馈投影明确区分 queued、applied 和 dismissed，并保留捕获时 baseline、来源 review 与结果 lineage，页面刷新后无需从 SSE 猜测是否已经生效。
- SSE 先按 durable event cursor 补进度，再附加可丢失 live prose。
- React effect、页面刷新、项目切换和 SSE 重连都不能 Start、Resume 或 Retry。
- 诊断 endpoint 只返回 task/attempt/profile fingerprint/usage/error metadata，不返回或解析正文 Blob。

## 9. 备份、恢复与导出

继续运行所需的备份是整个应用数据库的一致快照，不是复制 `.sqlite3` 主文件：

1. 服务停止或所有运行到达安全边界；
2. SQLite Online Backup API 创建快照；
3. 校验 integrity、foreign keys、已知且可升级的 schema revision 和每个 Blob hash；
4. 写入绑定文件大小、SHA-256、event sequence 和 Blob count 的 manifest。

迁移前备份允许处于当前 Alembic 迁移树中的旧 revision；它按备份自身 revision 校验，不会拿 head 表集合错误拒绝旧库。Restore 要求 FastAPI 已停止且无 WAL/SHM sidecar，先验证快照，再迁移 staging 库到 head，最后原子替换整库；不做项目行级 merge。

单本小说只提供 Markdown 导出。导出按 book ordinal 读取正式 Chapter baseline，计算 snapshot fingerprint 与 content hash；workspace 草稿、失败 attempt 和 live delta 永远不会进入正文。

## 10. 实验与真实观测

未来母本冻结会在明确节点把正式 baseline、Canon、任务证据 identity 和 manifest 发布为不可变 bundle。普通项目与母本在冻结前使用完全相同的领域模型和持久化规则。

首轮真实观测是独立的 post-acceptance series：同一 Prompt/hash、Profile/fingerprint、代码 source hash、Harness contract 和 actor policy，按 `full_auto → participatory → full_auto → participatory` 创建四个新项目。合法 Book/Arc 产品动作不算技术救援；runner 不调用 Retry/Resume，不修改模型输出、Prompt、Profile 或数据库。结果只形成事实报告和问题索引，不反向决定离线工程验收。
