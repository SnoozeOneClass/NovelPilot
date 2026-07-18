# Agent 稳定性测试记录

本文记录真实长篇连续创作测试中，导致 Agent 自动重试、局部返修、人工重试或最终停止的原因。目的不是简单提高重试上限，而是区分故障层级，持续降低无效重试和整条创作链中断的概率。

## 统计快照

- 统计日期：2026-07-16
- 数据来源：`output/project-7e9e949c-eb43-4d5e-bccb-85d3901dd8d9/events.jsonl`
- 统计范围：事件 `seq=1..1833`；当时 Chapter 10 已完成候选提交并刚进入语义评测
- 样本性质：单个真实连续创作项目，不是独立重复实验

不同数字的统计单位不同，不能直接相加当作“失败次数”：

- Provider 重试：一次额外模型请求记一次；同一调用可能连续重试多次。
- Tool 拒绝：Harness 拒绝一次 Tool 提交记一次；Agent 通常会在同一 activation 内修正后重新提交。
- 语义局部返修：Evaluator 对一个候选返回一次 `local_repair` 记一次。
- 人工重试：用户显式准备一条新候选链记一次。
- 最终停止：一次 run 进入 `run_failed` 记一次。

## 当前统计

| 原因 | 数量 | 是否导致过最终停止 | 当前判断 |
| --- | ---: | ---: | --- |
| 模型服务或网络波动触发 Provider 自动重试 | 24 次额外请求，约分布在 14 个调用组 | 4 次历史停止 | 属于基础设施波动；最近已加入 action-local 重试与持久化等待 |
| Chapter 状态补丁的证据不是候选正文逐字子串 | 25 次 Tool 拒绝 | 1 次历史停止 | 当前最高频的可优化问题 |
| Chapter 语义审查要求局部返修 | 16 次 | 2 次耗尽局部返修预算 | 多数是 Harness 正常发挥作用，但高频说明生成上下文和提交协议仍可改善 |
| Story Arc 语义审查要求局部返修 | 1 次 | 0 | 正常审查行为，当前样本不足 |
| Book discussion Tool 参数不符合 schema | 1 次 | 0 | 低频格式问题 |
| 新候选与旧的不可变 evaluation 路径冲突 | 1 次 | 1 次 | 已由 revision-scoped evaluation 和事务化恢复修复 |
| 用户显式准备 Chapter 新候选链 | 2 次 | 不适用 | 分别发生在 Chapter 6 和 Chapter 9，是失败后的操作，不是根因 |

### Provider 重试分布

- Chapter Agent：19 次额外请求。
- Chapter Evaluator：3 次额外请求。
- Book discussion：2 次额外请求。

历史上导致 run 最终停止的 Provider 问题包括：

1. `503 auth_unavailable`：模型认证或配置不可用。这不是瞬时网络错误，应该停止并要求修正配置。
2. `500 ... responses: EOF`：上游连接在响应完成前中断，共出现两次最终停止。
3. `SSL: UNEXPECTED_EOF_WHILE_READING`：TLS 连接异常中断，出现一次最终停止。

最近的 Chapter 10 已观察到一次 `agent_transport_retry`，随后同一 activation 正常完成规划、流式正文和候选提交。这说明新的请求级自动重试至少已经在真实链路中成功恢复过一次。当前日志还没有出现 `waiting_for_provider`，因此“即时重试耗尽后进入持久化等待、服务恢复后自动唤醒”仍需要后续真实波动验证。

### Tool 提交拒绝

共记录 26 次失败的 Tool 调用：

- `submit_chapter_candidate / candidate_patch_evidence_not_verbatim`：25 次。
- `submit_book_discussion_update / invalid_tool_arguments`：1 次。

证据逐字匹配失败表示：Agent 在候选状态补丁中声称某条事实有正文证据，但提交的引用不是当前候选正文中的精确原文。常见风险包括引用被改写、标点或空白不一致、引用了计划而非正文，以及引用了尚未批准或不存在的正式文件。

这项校验本身不能移除，否则状态事实可能失去可追溯性。优化方向应是减少 Agent 提交错误：

- 在 Tool 描述和修复反馈中更明确地区分“正文原文”“计划”和“既有正式状态”。
- 在提交前提供确定性的引用定位/校验能力，让 Agent 选择实际存在的正文片段。
- 只重做被拒绝的 evidence index，不重跑已完成的章节规划和正文生成。
- 单独统计首次提交通过率、平均修正轮数和修正后成功率。

### 语义局部返修

Chapter Evaluator 共返回 16 次 `local_repair`，产生 27 条具体 issue：

- 证据、公平推理、揭示时机和观察边界：19 条。
- 契约、状态补丁或上层上下文缺失/不一致：5 条。
- 视角、内部一致性和场景目标覆盖：3 条。

这说明 Harness 当前主要拦截的是项目真正关心的长篇推理稳定性，而不是泛化文学打分。它们不能全部视为系统 bug，但以下现象需要继续优化：

- Chapter 6 和 Chapter 9 各有一次局部返修预算耗尽。
- Chapter 9 的问题同时涉及正文证据逻辑与状态补丁溯源，说明“正文修订”和“提交协议修订”仍可能被混在同一返修链中。
- issue category 存在命名不统一，例如 `fair_play_rule_planting` 与 `fair-play evidence setup`，不利于后续聚合分析。

后续应先提高可观测性，再决定是否调整预算：

- 规范 Evaluator issue category 枚举，禁止模型自由创造同义类别。
- 区分正文缺陷、上下文缺失、状态补丁缺陷和 Reviewer 自身误判。
- 统计每类问题的首次出现章节、自动修复成功率、平均返修轮数和最终是否升级。
- 对高频类别检查 Chapter Agent 获得的 Story Arc 契约、Book Direction 和已提交状态是否完整、明确且无冲突。

### 章节热点

| 章节 | Provider 额外请求 | 证据逐字匹配拒绝 | 语义局部返修 | 其他情况 |
| --- | ---: | ---: | ---: | --- |
| Chapter 2 | 10 | 11 | 5 | 4 次历史最终停止，问题最集中 |
| Chapter 6 | 6 | 1 | 4 | 1 次返修耗尽、1 次人工重试、1 次旧 evaluation 冲突 |
| Chapter 9 | 0 | 6 | 4 | 1 次返修耗尽、1 次人工重试 |
| Chapter 1 | 4 | 1 | 1 | 其中 3 次重试来自不可重试的认证错误 |
| Chapter 10 | 1 | 0 | 0（统计时尚在执行） | 已观察到自动重试后继续生成 |

## 已实现与仍待实现

### 已实现的稳定性改进

- `RunHost` 接管连续创作推进，浏览器不再承担任务存活责任。
- Provider 即时重试预算按逻辑 action 独立计算，不跨章节累计。
- 瞬时 Provider 故障耗尽即时预算后可进入持久化 `waiting_for_provider`，由后端退避唤醒。
- 配置、认证等确定性错误与瞬时网络错误分流。
- candidate、evaluation、verification 和 promotion 使用稳定 action identity、fingerprint 与事务恢复。
- evaluation 改为 revision-scoped，修复“新候选撞上旧不可变 evaluation”的问题。
- 真正停止且允许重试时保留显式“重试当前步骤”作为运维恢复入口。

### 仍待实现或验证

1. **证据引用提交优化**：25 次拒绝是当前最明确的高频工程改进点。
2. **语义返修分类与指标面板**：先确认返修是正文问题、上下文问题、状态补丁问题还是 Evaluator 误判。
3. **持久化 Provider 等待的真实验证**：实现已完成，但当前测试日志尚无一次完整的 `waiting_for_provider -> recovered` 现场。
4. **全书方向审查可见进度**：仍记录在 `docs/ux-follow-ups.md` 的 Backlog 中，尚未实现。
5. **实验母本冻结检查点可达性回归**：冻结要求“待测试故事弧已经批准、尚未开始章节且 Harness 空闲”，但当前故事弧批准接口会立即唤醒 `RunHost`，使该状态变成无法可靠操作的竞态窗口。后续应加入实验室专属的 freeze intent，并保证批准后先冻结、再继续连续创作；不得为普通创作重新增加通用安全暂停点。详细实现清单记录在 `.trellis/tasks/07-13-benchmark-fixture-freeze/implement.md`。
6. **Chapter 13 恢复后进入静默暂停**：事件 `seq=2141..2155` 显示候选状态补丁连续三次因 `candidate_patch_evidence_not_verbatim` 被拒绝，随后以 `tool_schema_repair_exhausted` 停止；`seq=2156` 的 `run_resumed` 没有产生后续 Harness action，`seq=2157` 的 `recover-stale` 又把项目置为 `paused`。此时运行意图仍显示 running，Creation 页面仍显示“正在进入下一个内部阶段”且没有恢复入口。需要统一 RunHost 接管确认、stale recovery 语义、readiness 与前端主状态，并让已耗尽的证据修正失败暴露真实可用的重试动作。详细清单记录在 `.trellis/tasks/07-14-agent-tool-architecture/implement.md` 的 Phase 16。

补充计数：原统计快照截止 `seq=1833`，当时证据逐字匹配拒绝为 25 次；截至这次 Chapter 13 现场的 `seq=2157`，累计已达到 32 次。最终结论仍以小说完成后的去重审计为准。

此前记录的“故事弧审批割裂正常创作流程”已经由 `.trellis/tasks/07-15-creation-workflow-ux` 实现；全书方向审查的可见进度记录仍然有效。

## 后续稳定性指标建议

后续每轮长篇测试至少保留以下指标，才能判断稳定性是否真的改善：

- 连续完成章节数，以及每章是否需要用户运维介入。
- Provider 调用总数、额外重试数、进入持久化等待次数、自动恢复率和恢复耗时。
- 各 Tool 首次提交通过率、自动修正成功率和修正轮数。
- 各语义 issue category 的出现次数、自动返修成功率和预算耗尽率。
- 每章从候选生成到正式提交的 action 数、模型调用数和最终耗时。
- 最终停止按 `transient_provider`、`deterministic_config`、`tool_protocol`、`semantic_exhausted`、`persistence_conflict` 分类后的数量。

稳定性的目标应定义为：瞬时服务波动和可局部修正的问题由系统自动吸收；确定性配置错误明确停止；真正需要用户创作决策时才请求用户，而不是依靠不断提高一个全局重试上限。

## 当前小说完成后的最终复盘

当前测试小说继续正常生成，本阶段不为了统计而暂停或修改运行链路。小说完整生成后，再对完整 `events.jsonl`、Agent activation、evaluation、候选修订和正式提交产物进行一次最终审计。

最终复盘至少应给出：

- 按 Book、Story Arc、Chapter 及具体章节统计 Provider 重试次数和涉及的独立调用数。
- 区分即时自动恢复、进入 `waiting_for_provider` 后恢复、最终停止，以及认证/配置类不可重试错误。
- 统计每种 Tool 拒绝原因、首次提交通过率、平均修正轮数和修正后成功率。
- 统计每种语义 issue category、`local_repair` 次数、自动返修成功率、预算耗尽次数和人工重试次数。
- 将一次根因事件与它触发的多次重试分开，避免把连锁事件重复解释成多个独立故障。
- 按章节标出热点，复核上下文缺失、正文缺陷、状态补丁错误、Evaluator 误判和 Harness 持久化问题各自所占比例。
- 对比稳定性架构调整前后的停止率和用户运维介入次数，再决定下一轮优化优先级，而不是仅按错误总数排序。

这份最终复盘将作为下一轮“让 Agent 稳定持续完成长篇小说创作”的优化依据；本文件中的当前统计只保留为中途快照。
