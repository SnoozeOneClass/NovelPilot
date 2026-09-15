# 模型配置

在 TUI 点击「设置」或按 F2 搜索“模型”。配置在同一页编辑：↑↓ / Tab 移动，←→ 切换选项，Enter 原位编辑，Ctrl+S 统一保存；Esc 取消。容量支持 `128K`、`1M`。未保存的编辑不会写入配置，已有密钥不回显，保存出错时表单保留供修正。

配置 API 位于 `cli/src/runtime/pi/model-config.ts`。它使用 Pi 0.85.1 的公开
`ModelRuntime`、`InMemoryCredentialStore` 和 `InMemoryModelsStore`，构建时不访问模型服务。

默认全局目录为 `~/.novelpilot`，测试或嵌入方可以显式指定其他目录：

- 全局模型：`~/.novelpilot/models.json`
- 本书模型：`<书目录>/meta/models.json`
- 专用凭证：`~/.novelpilot/credentials.json`

模型文件不接受 API Key、任意 HTTP headers 或其他未知字段。凭证是专用本地明文文件，
新建文件权限为 `0600`；Windows 的实际访问控制仍由目录 ACL 决定。不要分享此文件。

```json
{
  "version": 1,
  "models": {
    "default": {
      "provider": "my-provider",
      "id": "model-id",
      "api": "openai-completions",
      "baseUrl": "https://example.com/v1",
      "contextWindow": 32000,
      "maxTokens": 4000,
      "reasoning": false,
      "thinkingLevel": "off"
    }
  }
}
```

`default`、`planner`、`writer`、`editor` 保存完整模型配置。角色的解析顺序是本书角色、
全局角色、本书默认、全局默认；讨论和意见裁定仅使用本书默认、全局默认。
`resolve(role)` 返回配置、来源范围、命中的角色槽和文件路径，可直接用于界面显示。

`set(scope, slot, profile)` 替换对应完整配置，不修改凭证。省略 profile 明确移除该层覆盖。
`setApiKey(profile, key)` 单独保存凭证；`undefined` 保留、`null` 明确删除。
凭证绑定 Provider 名、协议和服务地址：改输出容量或模型 ID 不会清空凭证，改变地址需要
重新配置凭证，避免把原服务的密钥发给新服务。`hasApiKey` 只返回是否已配置。
保存以跨进程文件锁保护读改写，并原子替换单文件；占用时返回可重试错误。

`buildModelRuntime(role)` 返回 `modelRuntime`、`model`、`thinkingLevel`、`pricing` 和
`effective`。前两项可交给 `createRoleSession`，随后以
`session.setThinkingLevel(thinkingLevel)` 应用推理设置。每次构建是独立快照，配置保存
不会改变已有请求。TUI/Host 应在后续请求边界刷新，并显示实际请求模型。
已有会话切换时，使用 `configureRuntime(session.modelRuntime, role)` 把新模型及私有凭证
配置到同一个运行时，再调用 `session.setModel(result.model)` 和推理设置方法，不能仅把
另一个运行时的模型对象交给旧会话。

目前只接受 `openai-completions`、`openai-responses`、`anthropic-messages` 三种显式
API 协议和 API Key；不包含 OAuth、环境凭证自动导入或任意 Pi Provider 的可用性承诺。
上下文/输出限制和推理能力由用户声明，实际供应商支持、工具调用兼容性、推理级别映射及
鉴权仍需真实模型样例验证。无推理能力时仅允许 `off`，其余可配置强度为
`minimal`、`low`、`medium`、`high`，SDK 按协议适配。

可选 `pricing` 包含每百万 token 的 `input`、`output`、`cacheRead`、`cacheWrite` 四项
费率。未提供时返回 `pricing: null`。Pi 类型要求数字费率，内部注册使用的零值仅为占位；
应用不能把该情况下 SDK 的 usage.cost 当成免费或已知费用，更不能直接作为评测费用。
调用统计必须同时保留应用的 pricing 元数据。

自动测试覆盖层级解析、非法配置、凭证隔离与保留、并发保存及禁止网络条件下的公开 SDK
模型注册。它们不证明某个线上模型已经可用于小说创作。
