# NovelPilot

NovelPilot 是面向写作小白的全自动长篇小说 Agent Harness。用户输入一句话创意后，系统自动
完成设定、滚动规划、逐章写作、质量检查、有限返工、摘要维护、完结和 TXT/Markdown 导出。

工程核心是一个可长期运行、可观测、可验证、可恢复的串行 Agent Loop：

- Pure Route 只根据 SQLite 权威事实选择下一条 Instruction；
- Architect、Writer、Editor 使用 Pydantic AI Tool loop，角色 Tool 权限相互隔离；
- Tool 事务同时提交正文/事实、Checkpoint、幂等证据和 Domain Event；
- 单项目 lease、崩溃对账、有限重试和 Failure Arbiter 防止重复副作用与无限循环；
- 上下文预算包含 System Prompt、Tool schema、请求选项和输出预留，达到 85% 窗口或
  更严格的输出预算阈值后分级压缩，并恢复 Canon、章节计划、Review 和已完成 Checkpoint；
- Profile 能力、Provider、fallback、实际 token/缓存/延迟/费用均形成冻结证据；
- FastAPI、SSE、React 和 Headless/Eval 共用同一事实与事件序列。

本实现调研并独立复现了 Apache-2.0 项目 `ainovel-cli` 的核心架构思想。来源边界见
[reference-provenance.md](docs/reference-provenance.md)。

## 本地启动

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm.cmd --prefix frontend install
npm.cmd run backend:dev
```

另一个终端：

```powershell
npm.cmd run frontend:dev
```

打开 `http://127.0.0.1:5173`。页面唯一产品路径是一句话全自动创作。

## 配置模型

从 `config/llm-profiles.example.json` 创建 git ignored 的 `config/llm-profiles.local.json`，
然后执行：

```powershell
npm.cmd run profile:probe -- <profile-id> --require-tools
npm.cmd run profile:authoring-metadata -- <profile-id> --context-window 128000 --max-output-tokens 8192
```

第二个命令自动写入当前配置 fingerprint，不会打印或复制 API key。完整步骤见
[local-usage.md](docs/local-usage.md)。

## 本地门禁

```powershell
npm.cmd run test:fast
npm.cmd run authoring:eval:fake
npm.cmd run audit:secrets
```

真实 Eval 会产生费用，只能显式运行 `npm.cmd run authoring:eval:real -- --profile <id>`。
Fake Eval 只证明流程合同，不代表真实模型质量或稳定性。
