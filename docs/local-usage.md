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

前端会把 API 请求代理到 FastAPI 后端 `http://127.0.0.1:8000`。

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

1. 在项目选择器中新建或打开项目。
2. 选择 `full_auto` 或 `participatory`。
3. 配置并选择 LLM profile。
4. 完成全书 setup 对话。
5. 批准全书 setup。
6. 启动或恢复 harness。
7. 在三栏工作台中观察 loop 状态、模型可见输出、产物、上下文快照、审查、验证信号、patch 状态和路由决策。
8. 需要时随时提交反馈。反馈会在下一个安全 checkpoint 被处理。
9. 需要时导出全书。

导出文件写入：

```text
output/<小说名>/exports/manuscript.md
```

导出只包含已经提交的章节 `final.md` 文件。

## 本地项目数据

生成的小说项目保存在：

```text
output/<小说名>/
```

这个目录会被 git 忽略。它可能包含草稿、正式章节、审查结果、状态补丁、事件、导出文件和 smoke 报告。

## 质量门禁

发布变更前运行完整本地质量门禁：

```powershell
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run test
npm.cmd --prefix frontend run build
npm.cmd run acceptance
npm.cmd run audit:secrets
```

基于 fixture 的测试覆盖项目存储、profile 安全、LLM 适配器、事件 replay、运行控制、反馈路由、产物摘要、章节验证、状态补丁提交/拒绝、重试准备和全书导出。

## 真实 Provider Smoke 与文学审查

有真实 LLM profile 可用时，运行：

```powershell
npm.cmd run smoke:live -- --profile-id main
```

该命令会在 `output/` 下创建带时间戳的 smoke 项目，完成 setup，运行一个有界的全自动章节 loop，导出 manuscript，并写入：

```text
exports/live_smoke_report.json
```

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
