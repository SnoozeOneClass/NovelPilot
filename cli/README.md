# NovelPilot CLI

基于 Pi SDK 和 TUI 的本地小说 Agent。当前已建立运行基础，讨论、自动创作及改稿流程仍在实现中。

Node.js 22.19.0 以上。在本目录安装和构建：

```powershell
npm.cmd ci --ignore-scripts
npm.cmd run build
```

切换到准备存放一本书的目录，再启动已构建入口：

```powershell
node E:/project/NovelPilot/cli/dist/main.js
```

无参数打开 TUI，`/quit` 或 Ctrl+C 退出。`--check-startup` 可在非交互终端检查作品目录、安装资源与目录占用。
每次启动只绑定当前目录。换书时退出程序，切换目录后重新启动。

开发时可在作品目录执行 `npm.cmd --prefix E:/project/NovelPilot/cli run dev`，开发入口会保留原始目录。
内置资源从安装位置读取，作品内容保存在启动目录；启动无需访问模型。

开发检查：`npm.cmd run typecheck`、`npm.cmd test`、`npm.cmd run smoke:pack`。
打包检查会在系统临时目录安装本地压缩包，不发布到 npm；首次可能下载缺失依赖。
