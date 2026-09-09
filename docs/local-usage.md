# NovelPilot 本地使用

## 安装与启动

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm.cmd --prefix frontend install
npm.cmd run backend:dev
```

另一个终端运行 `npm.cmd run frontend:dev`，访问 `http://127.0.0.1:5173`。
FastAPI 启动时自动应用 `backend/app/authoring/store/migrations/`，权威库默认是
`data/authoring.sqlite3`。本分支不读取或迁移旧 NovelPilot 数据。

## Profile 与 Authoring metadata

页面右上角的“模型设置”提供连接配置、能力验证和默认模型选择。已有模型会显示保存的
连接参数，API key 字段保持空白；编辑时留空会沿用已保存的密钥。修改连接、请求选项或
密钥后需重新验证。上下文窗口、最大输出和价格位于高级设置，保存后再执行连接验证。

也可以使用命令行配置：

1. 复制 `config/llm-profiles.example.json` 为 git ignored 的
   `config/llm-profiles.local.json`，填写本地 API key、协议、base URL、opaque model id 和请求选项。
2. 用生产 Adapter 写入 capability evidence：

   ```powershell
   npm.cmd run profile:probe -- <profile-id> --require-tools
   ```

3. 原子写入 secret-free 上下文与价格元数据：

   ```powershell
   npm.cmd run profile:authoring-metadata -- <profile-id> `
     --context-window 128000 `
     --max-output-tokens 8192 `
     --input-price 1.25 --output-price 5.00 --cache-price 0.50
   ```

metadata 命令自动使用当前 Profile configuration fingerprint，保留其他条目，不访问网络，
不打印或存储 API key。若 Profile 配置了 `request_options.max_tokens`，它必须与
`--max-output-tokens` 相同。价格单位为每百万 token，未知时可省略并记为零。

上下文窗口与单次最大输出分开配置。系统在估算输入达到窗口的 85% 时提前压缩；至少
预留 8000 tokens，输出上限加安全余量更大时继续提前。1M 窗口与 65536 输出上限对应
850000 tokens 压缩阈值。预留空间也供后续消息和 Tool 结果增长使用，不是要求模型输出
固定长度。

Anthropic 1.3 使用独立的 `httpx2` observed transport，与 OpenAI 的 `httpx` transport
产生相同的物理请求、响应头和流式证据。当前 SDK 已移除 `temperature`、`top_p`、`top_k`
参数；若把这些字段写入 Anthropic Profile，绑定会在零 Provider 请求时明确失败，而不会
静默忽略。

## 使用

浏览器只需输入故事想法；目标章节数/字数和分角色模型均为可选设置。启动后可查看进度、
当前阶段，执行暂停、继续、取消，并下载 TXT 或 Markdown。完成后可直接预览正文。
作品暂停后可调整模型绑定，继续创作时使用新绑定。刷新和 SSE 重连不会改变运行路线。

Headless Fake 运行：

```powershell
scripts\python.cmd -m app.authoring.entry.headless --brief "一个守钟人的承诺" --target-chapters 3 --fake
```

Fake Eval：

```powershell
npm.cmd run authoring:eval:fake
```

真实 Eval 会调用付费 Provider，只在明确决定后执行：

```powershell
npm.cmd run authoring:eval:real -- --profile <profile-id> --report-dir data\authoring-eval\real
```

## 本地文件

```text
config/llm-profiles.local.json                 # Profile 与 API key
config/authoring-model-metadata.local.json     # 无密钥窗口/价格/fingerprint
data/authoring.sqlite3                         # 唯一权威事实库
data/authoring-eval/                           # Eval 报告
output/                                        # 可重新生成的导出
```

运行 `npm.cmd run audit:secrets` 可确认密钥未进入数据库、导出或报告。
