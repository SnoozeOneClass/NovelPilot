# 本地使用

## 安装

在仓库根目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm.cmd --prefix frontend install
```

仓库根目录的 npm 脚本通过 `scripts\python.cmd` 选择解释器：优先使用标准
venv 的 `.venv\Scripts\python.exe`，其次使用 Conda 前缀的
`.venv\python.exe`，两者都不存在时使用当前 PATH 中已激活的 `python`。
Python 环境不会随 Git 仓库同步，因此每台电脑需要单独创建环境或激活已有环境。

## 启动应用

分别在两个终端启动后端和前端：

```powershell
npm.cmd run backend:dev
```

```powershell
npm.cmd run frontend:dev
```

打开：

```text
http://127.0.0.1:5173
```

前端会把 API 请求代理到 FastAPI 后端 `http://127.0.0.1:8010`。

## 配置 LLM Profile

可以在应用里的 LLM Profiles 面板配置，也可以用 PowerShell 配置：

```powershell
$env:NOVELPILOT_API_KEY = "<your-api-key>"
npm.cmd run profile:upsert -- --id main --name "Main Provider" --protocol openai-compatible --base-url "https://api.example.com/v1" --model "model-name" --api-key-env NOVELPILOT_API_KEY --request-options-json '{"reasoning_effort":"high"}' --select
```

支持的协议：

- `openai-compatible`
- `anthropic-compatible`

Profile 保存在 `config/llm-profiles.local.json`，这个文件会被 git 忽略。生成的小说项目只保存脱敏后的 profile/model 快照。

设置页的“额外请求参数（JSON）”会把任意 Provider 请求体字段合并进正式调用，例如 `reasoning_effort`、`temperature`、`max_completion_tokens` 或 Provider 私有扩展。Novelpilot 只固定保护 profile 选择的 `model`、已经装配的 `messages/system` 与默认开启的 `stream`，不再自动注入温度、输出 token 上限或 `response_format`。Anthropic 兼容接口通常要求 `max_tokens`，需要在 profile 中显式填写。扩展参数不是秘密存储区，不要把额外密钥写在这里。

所有正式模型调用默认读取流式响应，并通过 SSE 展示正文增量或结构化任务的接收进度。应用层不设置总请求超时；Provider、代理、操作系统或网络仍可能主动断开连接。CLI 更新 profile 时，省略 `--request-options-json` 会保留原有扩展参数。

测试已保存的 profile：

```powershell
npm.cmd run profile:test -- --profile-id main
```

## 新建并创作小说

1. 在项目选择器中选择“开始新书”或“继续创作”。
2. 开始新书时选择 `full_auto` 或 `participatory` 初始模式；新项目先显示为“未命名新书”，无需预先输入书名。继续创作会恢复已有项目的内容、进度和模式。
3. 配置并选择 LLM profile。
4. 在“共创”中自由讨论全书方向。模型会持续维护完整草稿、已确认决定、已取代决定、待定项、假设和矛盾；讨论轮数不受限制。推荐回复会追加到未发送输入，不会覆盖已经写下的内容。
5. 方向成熟后点击整理并审阅。候选必须逐项覆盖并结构化保留已确认决定，同时给出若干带理由的推荐书名；审阅通过后选择推荐书名或输入自定义书名，再明确批准当前候选版本。正式标题与全书方向会一起提交，有阻断问题时继续讨论后重新审阅。
6. 启动或恢复 harness。
7. 在“工作台”中观察三栏 loop 状态、模型可见输出、产物、上下文快照、审查、验证信号、patch 状态和路由决策；在“故事世界”查看故事弧、章节和正史，在“证据中心”查看完整审计轨迹。参与模式审批故事弧时，可以在 1～30 章范围内调整 Story Arc Loop 给出的建议章节数。
8. 需要时随时提交反馈。反馈会在下一个安全 checkpoint 被处理。
9. 需要时导出全书。

项目打开后可以切换全自动或参与模式。运行中的原子动作不会被中断，必须先等待安全 checkpoint；已经等待人工审批的故事弧不会因为切到全自动而被跳过。当前故事弧状态缺失或无法验证时，系统会拒绝切到全自动。

导出文件写入：

```text
output/project-<项目 ID>/exports/manuscript.md
```

导出只包含已经提交的章节 `final.md` 文件。

## 制作消融实验母本

“实验室”是常驻的独立入口，不是正常三层 Loop 的必经阶段。用于生成建议测试故事的 Prompt 与冻结步骤见 [消融实验母本故事 Prompt 1](benchmark-story-prompt.md)。

母本身份只能在新建小说时声明：先选择“参与模式”，再勾选“创建实验母本项目”。母本项目在冻结前仍走完全相同的全书、故事弧和章节流程，但创作模式会保持为参与模式，不能中途取消母本身份、改为全自动，也不能把已有普通小说转换为母本。

固定冻结点是：第一故事弧已完成并提交章节，第二故事弧已完成规划、正在等待人工审批，而且第二故事弧尚未开始写章。批准第二故事弧时，后端会先停止持久运行意图，再提交批准并自动发布母本；不会为第二故事弧分配章节或发起新的模型请求。

母本写入：

```text
output/experiments/fixtures/fixture-<ID>/
```

该目录包含不可变快照、校验 manifest 和供未来 `none` 基线使用的 `direct_prompt.md`。发布成功后，源项目保留在普通项目列表中并标记为“已冻结母本”，可以打开、查看、导出、进入实验室或按普通项目删除，但不能继续生成或修改。

若发布失败，第二故事弧批准不会回滚，源项目仍保持停止和只读。“实验室”会显示失败原因并提供本地发布重试；该重试不调用模型、不重新审批，也不改写全书、故事弧、正史或已提交章节。相同检查点重复发布只返回已校验的现有母本。

## 本地项目数据

生成的小说项目保存在：

```text
output/project-<项目 ID>/
```

这个目录会被 git 忽略。它可能包含草稿、正式章节、审查结果、状态补丁、事件、导出文件和 smoke 报告。

全书讨论的主要文件是：

```text
book/setup.json                         # 当前讨论状态
book/direction_draft.md                 # 最新候选方向草稿
book/discussion/transcript.jsonl        # 完整本地讨论记录
book/discussion/turn-*/attempt-*/       # 上下文、响应及不可变状态/草稿/transcript
book/reviews/review-*/                  # 版本化候选、约束与审阅结论
book/direction.md                       # 明确批准后的正式方向
book/constraints.json                   # 明确批准后的结构化约束
book/outline.md                         # 滚动故事弧规划契约
```

讨论草稿和 `book/reviews/` 不会自动成为正式方向。继续讨论会使当前待批准候选失效，但旧审阅目录保留用于追溯。另一个进程产生的过期结果会因 revision 冲突被丢弃；批准时正式全书文件会一起事务提交，不会留下半套已批准状态。

## 质量门禁

发布变更前运行完整本地质量门禁：

```powershell
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run test
npm.cmd run frontend:build
npm.cmd run acceptance
npm.cmd run audit:secrets
```

基于 fixture 的测试覆盖开放式全书讨论、延迟命名与候选书名、上下文预算与版本追溯、确认决定覆盖、候选隔离、审阅阻断、并发冲突、多文件事务回滚、版本化批准、安全模式切换、profile 安全、LLM 适配器、事件 replay、运行控制、章节验证、状态补丁提交/拒绝、重试准备和全书导出。

## 真实 Provider Smoke 与文学审查

有真实 LLM profile 可用时，运行：

```powershell
npm.cmd run smoke:live -- --profile-id main
```

该命令会在 `output/` 下创建稳定内部 ID 的 smoke 项目，提交一份完整创作意图，执行开放讨论、包含推荐书名的候选综合、独立审阅和明确批准，然后运行一个有界的全自动章节 loop，导出 manuscript，并写入：

```text
exports/live_smoke_report.json
```

候选审阅阻断时，Smoke 会把问题重新注入讨论并重试，最多进行三次整理审阅。这个上限只用于自动化 smoke；应用内的真实全书讨论没有轮数上限。

检查生成的 `final.md`、`review.md`、`verification.json` 和状态补丁文件后，记录人工审查结果：

```powershell
npm.cmd run review:literary -- --project "<smoke-project-path>" --decision approved --chapter-assessment "<notes>" --state-patch-assessment "<notes>"
```

然后审计完成度：

```powershell
npm.cmd run audit:completion -- --project "<smoke-project-path>"
```

只有静态验收、输出密钥审计、真实 provider smoke 和文学审查证据都通过时，完成度审计才会通过。

## 应留在本地的内容

这些路径不应进入公开 push：

```text
config/*.local.json
output/
node_modules/
.tmp/
cache directories
```

如果本地存在 Trellis 或 agent 工作区文件，除非你明确想公开这些流程历史，否则应该把它们保留在本地私有分支。
