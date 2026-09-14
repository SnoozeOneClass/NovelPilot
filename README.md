# NovelPilot

基于 TypeScript、Pi SDK 和 TUI 的本地小说 Agent，以 ainovel-cli 为交互与工作流参考。

产品围绕同一本书展开：先讨论人物、世界观与设定，用户明确开始后自动规划、写作和评审，写作中允许提出修改意见。
当前已实现运行基础和最小终端入口，完整创作流程仍在开发中。

## 设计原则

- 每次启动绑定当前作品目录；退出后切换目录即可换书。
- 讨论保留多轮上下文；规划、写作、评审按任务隔离，跨任务事实来自作品文件。
- 角色使用各自的小说工具，应用层负责权限、任务完成与持久化，Pi 负责角色内的模型和工具循环。
- 采用 Markdown、JSON、JSONL 文件方案；目录独占和恢复协议由应用管理。

## 开发与启动

需要 Node.js 22.19.0 或以上。在仓库根目录运行：

```powershell
npm.cmd run setup
npm.cmd run build
```

进入准备存放一本书的目录，再启动：

```powershell
node E:/project/NovelPilot/cli/dist/main.js
```

输入 `/quit` 或按 Ctrl+C 退出。当前入口不调用模型。
开发时可在作品目录使用 `npm.cmd --prefix E:/project/NovelPilot run dev`。

## 项目结构

- `cli/src/`：新版应用、Pi 适配、文件存储和终端界面。
- `cli/assets/`：随安装包交付的内置资源。
- `cli/tests/`：新版行为测试。
- `docs/`：使用说明、来源与验证证据。

根目录命令全部面向新 CLI。旧产品代码、依赖、数据与入口已移除，不提供旧版兼容或迁移。

详见 [使用说明](docs/local-usage.md)、[参考来源](docs/reference-provenance.md)、[验证证据](docs/engineering-evidence.md)。
