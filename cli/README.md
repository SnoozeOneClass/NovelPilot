# NovelPilot CLI

TypeScript + Pi SDK 小说 TUI。讨论人物和设定后明确开始，系统自动规划、写作、评审和必要返工。
固定在项目根目录启动，新书直接进入共创，不要求书名。未命名草稿由程序保存，点击开始创作时再选择或生成书名，整理到 `books/<书名>/`；内置资源从安装包读取。

Node.js >=22.19.0。在本目录执行 `npm.cmd ci --ignore-scripts`、`npm.cmd run build`。
在项目根目录运行 `npm.cmd run dev`，选书后点击「设置」配置模型。
共创双栏、写作三栏，提供底部按钮和 F2 搜索菜单；共创时 Tab 切换对话/草稿焦点。模型配置单页原位编辑、统一保存；斜杠命令仅为可选快捷方式。

开发检查：`npm.cmd run typecheck`、`npm.cmd test`、`npm.cmd run eval`、`npm.cmd run smoke:pack`。
默认 eval 为确定性流程验证；`--real` 使用配置的真实模型。
