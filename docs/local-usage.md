# 本地使用

## 安装

在仓库根目录运行：

```powershell
python -m pip install -e .[dev]
npm.cmd --prefix frontend install
```

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
npm.cmd run profile:upsert -- --id main --name "Main Provider" --protocol openai-compatible --base-url "https://api.example.com/v1" --model "model-name" --api-key-env NOVELPILOT_API_KEY --select
```

支持的协议：

- `openai-compatible`
- `anthropic-compatible`

Profile 保存在 `config/llm-profiles.local.json`，这个文件会被 git 忽略。生成的小说项目只保存脱敏后的 profile/model 快照。

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
7. 在“工作台”中观察三栏 loop 状态、模型可见输出、产物、上下文快照、审查、验证信号、patch 状态和路由决策；在“故事世界”查看故事弧、章节和正史，在“证据中心”查看完整审计轨迹。
8. 需要时随时提交反馈。反馈会在下一个安全 checkpoint 被处理。
9. 需要时导出全书。

项目打开后可以切换全自动或参与模式。运行中的原子动作不会被中断，必须先等待安全 checkpoint；已经等待人工审批的故事弧不会因为切到全自动而被跳过。当前故事弧状态缺失或无法验证时，系统会拒绝切到全自动。

导出文件写入：

```text
output/project-<项目 ID>/exports/manuscript.md
```

导出只包含已经提交的章节 `final.md` 文件。

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
