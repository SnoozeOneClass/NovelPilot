# 本地使用与开发

## 当前范围

目前提供 TUI 基础入口、目录独占、Pi 角色会话和文件写入基础。开书讨论、自动写作、修改意见、Skills、导出和评测尚未接入完整用户流程。

## 安装

Node.js >=22.19.0。在仓库根目录执行 `npm.cmd run setup`，使用 `cli/package-lock.json` 安装锁定依赖；再执行 `npm.cmd run build`。
新版产品不需要 Python、Web 服务或数据库。Trellis 工作流工具独立于产品运行。

## 作品目录

在作品目录执行 `node E:/project/NovelPilot/cli/dist/main.js`。TUI 使用启动目录作为书目录，并在其生命周期内持有 `.novelpilot.lock` 的操作系统锁。
锁文件会保留；不要把它是否存在当作占用判断，也不要删除正在使用的锁文件。

`--check-startup` 可检查书目录、内置资源和目录占用后退出，不调用模型。无参数启动需要交互终端。
输入 `/quit` 或按 Ctrl+C 退出；换书时退出、切换目录、重新启动。

内置提示词从安装包读取，不从书目录或旧项目配置读取。开发入口通过 npm 的原始启动目录定位书稿。
旧书稿、数据库和凭证已按用户决定删除；新版从独立的新书目录开始，不提供旧数据兼容和迁移。

## 开发检查

在仓库根目录执行：

```powershell
npm.cmd run check
npm.cmd run smoke:pack
```

`check` 依次执行新版类型检查、测试和构建。`smoke:pack` 将本地包安装到系统临时目录并从独立书目录启动，可能下载缺失依赖，不发布到 npm。
也可以进入 `cli/` 使用其中的独立命令。
