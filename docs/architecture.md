# Novelpilot 架构说明

## 产品目标

Novelpilot 是一个本地、单用户的 AI 长篇小说创作 Web 应用。

它不是为了演示“LLM 能写一段小说”，而是为了展示一个可以长期运行的 Agent Loop Harness：系统需要保存目标、控制状态边界、产生可观察证据、执行验证、处理纠偏，并能从失败或重启中恢复。

第一版明确不处理云端复杂度：不做账号系统、不做多人协作、不做远程同步，也不要求托管部署。

## 运行结构

```text
React/Vite 前端
  |
  | HTTP 命令：项目新建/打开/关闭、profile CRUD、setup、运行控制、反馈、导出
  | SSE 流：harness 运行事件和模型可见输出
  v
FastAPI 后端
  |
  +-- Project service：管理 output/project-<项目 ID>/ 生命周期
  +-- LLM profile service：管理被 git 忽略的本地 LLM 配置
  +-- LLM gateway：OpenAI 兼容和 Anthropic 兼容适配器
  +-- Harness orchestrator：全书 loop、故事弧 loop、章节 loop
  +-- Storage service：文档产物、JSON 状态、事件、重试、导出
```

所有文件系统写入都由后端负责。前端是工作台：用户在这里选择项目、配置 profile、共创并批准全书方向、控制运行、查看 harness 证据、提交反馈，并导出已提交章节。

前端按五个任务域组织：`共创`、`工作台`、`故事世界`、`证据中心`、`实验室`。其中实验室是常驻但独立于正常三层 Loop 的实验基础设施入口；设置不占用任务域导航。服务端状态由 TanStack Query 管理，SSE 层负责事件去重并只失效相关查询；长期事件、产物和章节列表使用 TanStack Virtual。主题通过同一套语义 Token 支持系统、亮色和暗色，不为不同主题复制组件样式。功能组件使用 CSS Modules，全局样式只保留 Token 和 Reset。

## 三层 Loop

Novelpilot 把长篇写作建模为三层嵌套 loop：

- 全书 loop：维护长期类型承诺、读者承诺、主角方向、世界约束、结局倾向和用户的主要创作意图。
- 故事弧 loop：滚动规划当前故事弧，管理多章累积效果、节奏、冲突推进、伏笔流动和阶段收束。
- 章节 loop：装配上下文，生成章节目标、草稿、候选观测、审查、验证、正式正文、候选状态补丁，并提交已验证的正史更新。

故事弧 loop 是滚动推进的，只规划当前故事弧，不要求一开始就生成整本书的完整路线图。一个故事弧结束后，系统会基于已提交正史、已批准的全书方向、前文、验证信号和待处理用户反馈来规划下一个故事弧。模型以结构化结果同时返回可读的 Markdown 计划和 1～30 范围内的建议章节数；全自动模式直接采用建议值，参与模式允许用户在批准当前故事弧时调整最终章节数。格式或范围无效时运行失败封闭，不使用固定章节数兜底。

## 实验母本

实验室当前提供消融实验的母本制作能力，不参与普通小说的推进。一个项目只有在全书方向已批准、至少一个故事弧及其章节已完整提交、当前故事弧已批准但尚未开始任何章节，并且没有 Harness 动作在运行时，才能冻结。

母本位于 `output/experiments/fixtures/fixture-<ID>/`，而不是源小说项目目录。后端按照固定白名单复制已批准的全书材料、相关故事弧计划和状态、预热章节的正式正文与已提交补丁，以及正史文件；草稿、失败尝试、讨论记录、运行锁和 LLM Profile 不会进入母本。每个文件由 manifest 记录大小与 SHA-256，manifest 自身也有校验侧车文件。

冻结还会确定性生成 `direct_prompt.md`，为未来完全不使用 Harness 的 `none` 基线提供与其他方案同源的扁平输入。检查点指纹用于去重：源项目未发生变化时重复冻结只返回已有母本。发布过程先写临时目录、完整校验后再原子移动，成功后在源项目事件流中留下不含秘密的审计事件。

## 全书方向共创与批准

全书方向是主要的深度 human-in-the-loop 阶段，不使用固定题库、固定顺序或问题数量。每轮讨论由模型结合当前草稿和讨论状态判断最关键的澄清点；模型可以给出用户口吻的参考回复，但自由输入始终存在。

每轮模型调用必须返回完整的候选 Book Direction 草稿，同时维护：

- 已确认决定。
- 被用户明确取代的历史决定及当前输入证据。
- 待澄清问题。
- 尚未确认的假设。
- 已发现但未解决的矛盾。
- 面向下一轮的紧凑讨论摘要。

Harness 只向模型注入完整方向草稿、维护后的摘要、决策状态和最近原始对话。更早的原始对话不会反复塞回 prompt，但完整 transcript 永久保留在本地。每个成功轮次保存不可变的草稿、讨论状态和 transcript 版本；上下文快照记录来源版本、注入内容的字符数与 SHA-256、摘要与排除项、总预算和装配理由，而不是复制一份完整 prompt。

模型不能静默删除已有确认决定。只有用户当前输入明确改变或撤销决定时，模型才能记录取代关系，并必须提供来自该次用户输入的逐字证据。候选综合还必须逐项给出确认决定的文本证据，并把原决定保留在结构化约束中；确定性门禁会独立检查这两件事，不能只依赖审阅模型自报通过。

模型给出的 `ready` 只是建议，不会自动结束讨论。用户主动请求审阅后，系统先综合候选方向、结构化约束和若干带理由的推荐书名，再发起一次独立语义审阅。冲突、把高影响假设写成事实、遗漏确认决定、内容无法约束后续创作、缺少滚动规划契约或提前写死未来全部故事弧都会阻止批准。用户可以选择推荐书名或输入自定义书名；只有用户明确批准当前候选版本，正式标题、`direction.md`、`constraints.json` 等全书产物才会在同一事务中提交。任何新讨论都会令当前候选失效，但不会删除旧审阅证据。

讨论与审阅结果使用 setup revision 做 compare-and-swap。另一个后端进程若已推进状态，旧模型结果会被丢弃并留下事件，不能覆盖新状态。正式批准及候选包通过可恢复的多文件事务提交；中途写入失败会回滚全部目标。事件追加按文件锁分配序号，失败后的 outbox 重放以 `event_id` 去重。

## 人类参与模式

开始新书时，用户选择一种初始运行模式：

- `full_auto`：故事弧计划和章节 loop 默认不需要故事弧级人工确认。
- `participatory`：每个故事弧计划在开始写章节前暂停，等待人工审查。

两种模式都允许用户随时提交反馈。反馈会立即记录，但不会打断正在进行的 LLM 原子动作；harness 会在下一个安全 checkpoint 处理反馈，并记录路由结果。

运行模式属于可变的路由策略。项目处于非运行状态时可以切换；`running` 或 `pause_requested` 时必须先等待安全 checkpoint。切到参与模式会让当前尚未批准的故事弧进入人工审查；切到全自动只影响后续无需新建人工门禁的路由，已经持久化为 `awaiting_review` 的当前门禁仍然有效，直到用户明确批准。若当前故事弧状态缺失或无法验证，切换会失败封闭，不能借此绕过门禁。

## 候选状态与已提交状态

核心安全规则是：LLM 输出默认都是候选材料，在 harness 验证并提交前不是正史。

这条规则同样适用于全书层：讨论草稿、模型综合结果和审阅包都不是已批准方向。模型调用失败或审阅失败不会改变正式全书产物，也不能自动放行。

每个章节会产生这些核心产物：

```text
context_snapshot.json
goal.md
draft.md
observations.json
review.md
verification.json
candidate_state_patch.json
committed_state_patch.json
final.md
```

关键边界如下：

- `draft.md` 是候选正文。
- `observations.json` 是从草稿中提取的候选观测，不是正史状态。
- `final.md` 只在验证通过后写入。
- `candidate_state_patch.json` 在正式正文存在后由 LLM 提出。
- `committed_state_patch.json` 只在 harness 校验通过后写入。
- 正史文件只能通过已提交 patch 更新。

这样可以防止状态污染。例如，某个被拒绝的草稿写了“重要角色死亡”，这个事件不能因为出现在候选观测里就进入正史。

## 正史与存储

小说项目保存在 `output/project-<项目 ID>/` 下。目录名来自稳定内部身份，正式标题稍后由全书 loop 决定，不参与路径计算。新项目会先在内部暂存目录完整初始化，成功后再原子发布到最终目录；应用启动和项目加载边界会恢复被进程中断的多文件事务，避免暴露半成品状态：

```text
project.json
events.jsonl
book/
  setup.json
  direction_draft.md
  discussion/
    transcript.jsonl
    turn-0001/attempt-001/
      context_snapshot.json
      response.json
      direction_draft.md
      state.json
      transcript.jsonl
  reviews/
    review-0001/
      attempt-001/
        context_snapshot.json
      candidate_direction.md
      candidate_constraints.json
      candidate_titles.json
      rolling_plan.md
      verification.json
      state.json
      transcript.jsonl
  direction.md
  constraints.json
  settings.md
  outline.md
  state.json
  feedback.md
arcs/
  arc-001/
    plan.md
    revision.md
    state.json
chapters/
  chapter-001/
    attempts/
    context_snapshot.json
    goal.md
    draft.md
    observations.json
    review.md
    verification.json
    final.md
    candidate_state_patch.json
    committed_state_patch.json
canon/
  characters.json
  relationships.json
  world_facts.json
  foreshadowing.json
exports/
  manuscript.md
  live_smoke_report.json
  literary_review.json
```

全书 manuscript 是导出产物，不是实时状态。它只由已提交的 `final.md` 章节拼接生成。

`project.json` 保存稳定项目 ID、可为空的正式标题和当前运行策略。`settings.md` 与 `direction.md` 保存同一份已批准方向，前者作为现有下游上下文入口；`outline.md` 保存滚动故事弧规划契约，不保存预先写死的全书路线图。故事弧规划和章节上下文都会读取这份已批准契约。当前流程只在批准全书方向时写入正式标题；项目目录始终由项目 ID 决定，因此确定标题不会移动目录或改变项目 ID。

## 上下文快照

`context_snapshot.json` 是审计产物，不是原始 prompt dump。章节 loop 和全书讨论都会记录使用了哪些上下文来源、可重建的不可变版本、注入块的字符数和哈希、哪些内容被摘要或排除、总字符预算，以及 harness 为什么这样装配上下文。

这是项目的核心卖点之一：前端可以展示 harness 如何控制模型看到的内容。

## LLM Profile 与密钥安全

LLM profile 是全局本地配置，不属于小说项目数据。它们保存在：

```text
config/llm-profiles.local.json
```

Profile 支持：

- `openai-compatible`
- `anthropic-compatible`

Gateway 的核心请求只负责模型、已装配消息和流式开关。所有正式调用默认流式读取，应用层不设置总超时，也不统一强制温度、输出 token 上限或 `response_format`。Profile 可以保存任意 JSON `request_options` 并合并进 Provider 请求体；调用级参数可以覆盖 profile 扩展参数，但不能替换 profile 选择的 `model`、Harness 已装配的 `messages/system` 或关闭 `stream`。因此不同模型可以自行配置推理强度、采样、上限和私有扩展，Anthropic 所需的 `max_tokens` 也由对应 profile 显式声明。

结构化 Harness 动作仍通过 prompt、解析器和候选产物 schema 约束结果，但这些属于业务契约，不再通过所有 Provider 都未必兼容的传输字段强制实现。自由文本动作可以把增量作为模型可见输出；全书讨论和故事弧计划等结构化动作只发布累计接收字符数，解析成功后再展示正式产物，避免把半截 JSON 当成小说内容。Provider 扩展参数会返回本地设置界面，不应承载额外秘密。

小说输出可以记录脱敏后的来源信息，例如 `profile_id` 和 `model_snapshot`，但不能保存 API key、原始 base URL、请求头或 provider 配置。分享输出前，可以用密钥审计命令扫描生成目录。

## 事件、恢复与运行控制

`events.jsonl` 是持久化的 harness 审计流。SSE 会把实时事件暴露给前端。

运行控制由后端 `RunHost` 持续推进，浏览器只负责观察和显式命令：

- 暂停请求不会取消正在进行的 LLM 动作。
- 暂停会在下一个安全 checkpoint 生效。
- 用户第一次点击“开始创作”后，后端会连续推进内部 checkpoint；页面刷新、切换路由或关闭浏览器不会承担续跑职责。
- 每个原子动作开始前写入 checkpoint，并且必须留下新的持久事件；没有状态进展的动作会被保护性停止，避免忙循环。
- 恢复运行时读取已提交状态、候选身份和持久事件，而不是读取不完整的流式输出。已完成候选但缺少评测时，只补做评测，不重新生成正文。
- 本地后端重启后，`RunHost` 会核对 `book/harness/run-state.json`，自动唤醒仍声明为 `running` 的项目。
- 单次模型请求先进行有限次即时重试；瞬时网络失败耗尽后进入持久化 `waiting_for_provider`，按 10/20/40/80/160/300 秒退避自动重试，不要求用户守在页面点击恢复。
- 鉴权、配置、能力不支持和确定性校验失败不会伪装成网络等待，而是停止并给出对应的修正或检查动作。

候选、评测和跨 Loop 路由都有稳定身份和持久证据。跨 Loop 路由会先写入待处理文件，再调用上层 Agent；即使进程中断也会先重放该路由。验证失败或状态补丁被拒绝时可以开启新的有限修订，失败候选会归档到 `attempts/`，而不是删除证据或覆盖已提交正文。

## 完成证据

自动检查覆盖静态验收、类型检查、lint、测试、前端构建和输出密钥安全。最后两个门禁刻意保留为人工检查：

- 使用真实配置的 LLM profile 跑通完整流程。
- 审查生成章节和状态补丁是否具有文学可用性。

这些门禁会在 smoke 项目的 `exports/` 目录下产生本地证据。
