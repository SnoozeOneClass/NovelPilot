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
  +-- Project service：管理 output/<小说名>/ 生命周期
  +-- LLM profile service：管理被 git 忽略的本地 LLM 配置
  +-- LLM gateway：OpenAI 兼容和 Anthropic 兼容适配器
  +-- Harness orchestrator：全书 loop、故事弧 loop、章节 loop
  +-- Storage service：文档产物、JSON 状态、事件、重试、导出
```

所有文件系统写入都由后端负责。前端是工作台：用户在这里选择项目、配置 profile、完成 setup、控制运行、查看 harness 证据、提交反馈，并导出已提交章节。

## 三层 Loop

Novelpilot 把长篇写作建模为三层嵌套 loop：

- 全书 loop：维护长期类型承诺、读者承诺、主角方向、世界约束、结局倾向和用户的主要创作意图。
- 故事弧 loop：滚动规划当前故事弧，管理多章累积效果、节奏、冲突推进、伏笔流动和阶段收束。
- 章节 loop：装配上下文，生成章节目标、草稿、候选观测、审查、验证、正式正文、候选状态补丁，并提交已验证的正史更新。

故事弧 loop 是滚动推进的，只规划当前故事弧，不要求一开始就生成整本书的完整路线图。一个故事弧结束后，系统会基于已提交正史、已批准的全书方向、前文、验证信号和待处理用户反馈来规划下一个故事弧。

## 人类参与模式

全书 setup 是主要的深度 human-in-the-loop 阶段。它类似规划对话：系统一次询问一个决策，提供推荐选项，允许用户自定义回答，并在全书 loop 激活前要求用户批准。

项目创建时，用户选择一种运行模式：

- `full_auto`：故事弧计划和章节 loop 默认不需要故事弧级人工确认。
- `participatory`：每个故事弧计划在开始写章节前暂停，等待人工审查。

两种模式都允许用户随时提交反馈。反馈会立即记录，但不会打断正在进行的 LLM 原子动作；harness 会在下一个安全 checkpoint 处理反馈，并记录路由结果。

## 候选状态与已提交状态

核心安全规则是：LLM 输出默认都是候选材料，在 harness 验证并提交前不是正史。

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

小说项目保存在 `output/<小说名>/` 下：

```text
project.json
events.jsonl
book/
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

## 上下文快照

`context_snapshot.json` 是审计产物，不是原始 prompt dump。它记录本章使用了哪些上下文来源、这些来源的版本、哪些内容被直接注入、哪些内容被摘要注入、哪些内容被排除，以及 harness 为什么这样装配上下文。

这是项目的核心卖点之一：前端可以展示 harness 如何控制模型看到的内容。

## LLM Profile 与密钥安全

LLM profile 是全局本地配置，不属于小说项目数据。它们保存在：

```text
config/llm-profiles.local.json
```

Profile 支持：

- `openai-compatible`
- `anthropic-compatible`

小说输出可以记录脱敏后的来源信息，例如 `profile_id` 和 `model_snapshot`，但不能保存 API key、原始 base URL、请求头或 provider 配置。分享输出前，可以用密钥审计命令扫描生成目录。

## 事件、恢复与运行控制

`events.jsonl` 是持久化的 harness 审计流。SSE 会把实时事件暴露给前端。

运行控制是协作式的：

- 暂停请求不会取消正在进行的 LLM 动作。
- 暂停会在下一个安全 checkpoint 生效。
- 恢复运行时读取已提交状态和持久事件，而不是读取不完整的流式输出。
- 本地后端重启后，stale run recovery 可以把遗留的 `running` 或 `pause_requested` 元数据恢复到 `paused`。

验证失败或状态补丁被拒绝时可以重试。重试准备会把失败的候选产物归档到 `attempts/`，而不是删除证据。

## 完成证据

自动检查覆盖静态验收、类型检查、lint、测试、前端构建和输出密钥安全。最后两个门禁刻意保留为人工检查：

- 使用真实配置的 LLM profile 跑通完整流程。
- 审查生成章节和状态补丁是否具有文学可用性。

这些门禁会在 smoke 项目的 `exports/` 目录下产生本地证据。
