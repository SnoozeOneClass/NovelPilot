# NovelPilot

使用 TypeScript、Pi SDK 和 TUI 构建的本地小说 Agent。先讨论人物、世界观和设定，再由系统自动规划、写作和评审；写作中可以提出修改意见。

## 设计

- 固定从项目目录启动，新书直接共创，不要求预先起名；已有作品可以选择继续。确定书名后统一整理到 `books/<书名>/`。
- 共创阶段使用“对话＋完整草稿”双栏，写作后进入三栏监控工作台；底部按钮和可搜索操作菜单提供当前阶段可用的操作。
- 讨论保留多轮上下文；规划、写作、评审按任务创建独立会话，从作品文件获取当前事实。
- 不同角色使用各自的小说工具。Pi 处理模型与工具循环，应用负责权限、任务调度和完成判定。
- 正文使用 Markdown，状态和检查点使用 JSON/JSONL。提交与人工改稿同步使用固定载荷和分阶段恢复。
- Skills 由模型按需选择并加载说明与资料。

## 安装与启动

需要 Node.js 22.19.0 或以上，在仓库根目录执行：

```powershell
npm.cmd run setup
npm.cmd run build
```

在项目目录启动程序：

```powershell
npm.cmd run dev
```

首次启动直接进入未命名共创。点击底部「设置」配置模型，再在输入区讨论人物和设定；准备好后选择「开始创作」。
没有预先确定书名时，模型提供候选，可直接采用推荐、自填或返回讨论。未命名草稿会自动保存，命名后连同本书配置一起归入书籍目录，同名目录不会覆盖。
常用操作可点击底部按钮或按 F2 搜索，用方向键和 Enter 选择；共创时 Tab 切换对话/草稿焦点。模型配置在单页原位编辑并统一保存，斜杠命令保留为快捷方式。

模型服务目前支持显式配置 OpenAI Chat Completions、OpenAI Responses 和 Anthropic Messages 协议。
真实服务的鉴权与生成效果取决于所配置的 Provider；当前验证情况见 [工程证据](docs/engineering-evidence.md)。

## 开发

产品实现位于 `cli/`，根目录命令全部指向新版：

```powershell
npm.cmd run check
npm.cmd run eval
npm.cmd run smoke:pack
```

例如《灯塔》的正文、计划、草稿和导出都位于 `E:/project/NovelPilot/books/灯塔/`，该目录默认不提交到 Git。
旧产品代码与数据已删除，不提供旧版兼容或迁移。

[操作说明](docs/local-usage.md) · [模型配置](docs/model-configuration.md) · [参考来源](docs/reference-provenance.md)
