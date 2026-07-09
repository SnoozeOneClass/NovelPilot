# Novelpilot

Novelpilot 是一个本地、单用户的长篇 AI 小说创作工作台。
它的核心不是简单地让模型连续写字，而是用三层 Agent Loop Harness 管住上下文、产物、验证、路由和正史提交。

- 全书 loop：确定经过批准的长期小说方向、设定和约束。
- 故事弧 loop：滚动规划当前故事弧，可按模式决定是否需要人工审查。
- 章节 loop：装配上下文，生成草稿，提取候选观测，进行语义审查、验证、正式章节提交、候选状态补丁生成，并由 harness 校验后提交到正史状态。

项目刻意保持 local-first。小说数据以文档和 JSON 文件的形式保存在 `output/<小说名>/` 下；LLM 密钥只存在被 git 忽略的本地配置文件里。

## 技术栈

- 后端：FastAPI、Pydantic、本地文件系统存储。
- 前端：React、Vite、TypeScript。
- LLM 协议：OpenAI 兼容和 Anthropic 兼容 profile。

## 文档

精简的公开设计文档见：

- [架构说明](docs/architecture.md)
- [本地使用](docs/local-usage.md)
- [Harness 设计原则](docs/harness-design-principles.md)

## 安装与启动

安装 Python 和前端依赖：

```powershell
python -m pip install -e .[dev]
npm.cmd --prefix frontend install
```

分别在两个终端启动后端和前端：

```powershell
npm.cmd run backend:dev
```

```powershell
npm.cmd run frontend:dev
```

打开应用：

```text
http://127.0.0.1:5173
```

前端会把 API 请求代理到 `http://127.0.0.1:8000`。

## LLM Profile

在应用里的 LLM Profiles 面板可以添加一个或多个 profile。一个 profile 包含：

- `id`
- `name`
- `protocol`：`openai-compatible` 或 `anthropic-compatible`
- `base_url`
- `api_key`
- `model`
- `enabled`

Profile 保存在：

```text
config/llm-profiles.local.json
```

这个文件会被 git 忽略。小说输出目录只保存脱敏后的 profile/model 快照，不保存 API key。

保存 profile 后，可以用 profile 行里的测试按钮先做一次小型 provider smoke test，再启动 harness。

也可以通过 PowerShell 创建或更新 profile，并避免把 API key 写进命令历史：

```powershell
$env:NOVELPILOT_API_KEY = "<your-api-key>"
npm.cmd run profile:upsert -- --id main --name "Main Provider" --protocol openai-compatible --base-url "https://api.example.com/v1" --model "model-name" --api-key-env NOVELPILOT_API_KEY --select
```

更新已有 profile 时，如果省略 `--api-key-env`，会保留已保存的 key，只修改 `--model` 或 `--base-url` 等非密钥字段。

运行完整 harness smoke 前，可以先在 CLI 测试已保存的 profile：

```powershell
npm.cmd run profile:test -- --profile-id main
```

省略 `--profile-id` 时会测试当前激活的 profile。命令会报告 provider/model 快照，并在成功或报错输出中隐藏所选 profile 的 key/base URL。相同的脱敏逻辑也会应用到 profile 测试 API 错误、live smoke 诊断和持久化的 `run_failed` 事件。

分享生成的小说项目前，可以扫描输出目录里是否误写入了已配置 profile 的 key 或 base URL：

```powershell
npm.cmd run audit:secrets
```

审计只报告文件路径、profile id 和值类型，不会打印原始 API key 或 base URL。

## 真实 Provider Smoke

配置真实 profile 后，可以运行完整的本地 harness smoke 测试：

```powershell
npm.cmd run smoke:live -- --profile-id <profile-id>
```

该命令会创建一个带时间戳的 `output/Novelpilot Live Smoke .../` 项目，选择指定 profile，回答 setup 对话，运行一个全自动章节 loop，导出 `exports/manuscript.md`，并写入 `exports/live_smoke_report.json`。除非传入 `--keep-active`，否则命令会恢复之前激活的项目和 profile。

批准前的 setup 对话可能包含 LLM 生成的追问。Smoke 命令会用有界循环回答这些问题，避免 provider 触发无限 setup 访谈。

成功输出会列出生成的 `final.md`、`review.md`、`verification.json` 和状态补丁文件，便于人工进行文学性和可用性检查。

检查这些文件后，记录人工审查结果：

```powershell
npm.cmd run review:literary -- --project "<smoke-project-path>" --decision approved --chapter-assessment "<notes>" --state-patch-assessment "<notes>"
```

也可以在应用中打开 smoke 项目，通过 Literary Review 卡片记录审查，并在右侧 Harness 面板检查完成门禁。

然后运行完成度审计：

```powershell
npm.cmd run audit:completion -- --project "<smoke-project-path>"
```

完成度审计也会扫描被审计的输出路径，检查是否泄露已配置 profile 的 API key 和 base URL。如果发现泄露，只报告 profile id、值类型和相对文件路径。

## 使用流程

1. 新建或打开一个小说项目。
2. 选择 `full_auto` 或 `participatory` 模式。
3. 配置并选择 LLM profile。
4. 完成 Book Setup 对话。
5. 批准全书 loop。
6. 启动或恢复 harness。
   全自动推进会跨过章节完成 checkpoint 继续运行，直到遇到人工门禁、失败、协作式暂停，或有界步数预算用尽，并停在安全 checkpoint。
   如果本地后端重启时项目元数据仍显示 `running` 或 `pause_requested`，可以使用 stale run recovery 把项目恢复到 `paused`，再从已提交状态继续。
7. 在三栏工作台中观察 loop 状态、模型可见输出、产物、路由、审查、验证信号和状态补丁结果。
   Harness 面板也会展示 setup、激活 LLM profile、运行控制和完成证据等 readiness gates。
8. 任何时候都可以提交反馈。反馈会立即记录，并在当前 LLM 原子动作结束后的下一个安全 checkpoint 被处理。
9. 如果验证或状态补丁校验失败，可以 retry 当前章节。Novelpilot 会先把失败候选产物归档到 `attempts/`，再重新生成。
10. 需要时导出全书。导出只使用已经提交的 `final.md` 章节。

## 存储模型

每个小说项目保存在 `output/<小说名>/` 下：

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
```

`events.jsonl` 是持久化的 harness 审计流。新追加的事件包含项目内局部递增的 `seq` 编号；旧的无序号事件仍然可以读取。

`draft.md`、`observations.json`、`candidate_state_patch.json` 等候选文件不是正史。只有通过 harness 校验并提交的 committed state patch 才会改变正史状态。

## 验证

运行完整本地质量门禁：

```powershell
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run test
npm.cmd --prefix frontend run build
npm.cmd run acceptance
npm.cmd run audit:secrets
```

当前基于 fixture 的测试覆盖项目存储、profile 安全、LLM 适配器、SSE replay、运行控制、反馈路由、产物摘要、章节验证、状态补丁提交/拒绝，以及全书导出。Acceptance report 会把已实现行为映射回规划需求，并把真实 provider 和文学审查保留为人工门禁。输出密钥审计由 `audit:secrets` 自动完成，并包含在 `audit:completion` 中。

当有可用的真实 LLM profile 时，运行：

```powershell
npm.cmd run smoke:live -- --profile-id <profile-id>
```

通过 CLI 或应用工作台记录文学审查后：

```powershell
npm.cmd run audit:completion -- --project "<smoke-project-path>"
```

该命令应报告所有完成门禁均通过，包括 `output_secret_audit`。
