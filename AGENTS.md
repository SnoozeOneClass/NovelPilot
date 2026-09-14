<!-- TRELLIS:START -->
# Trellis Instructions

These instructions are for AI assistants working in this project.

This project is managed by Trellis. The working knowledge you need lives under `.trellis/`:

- `.trellis/workflow.md` — development phases, when to create tasks, skill routing
- `.trellis/spec/` — package- and layer-scoped coding guidelines (read before writing code in a given layer)
- `.trellis/workspace/` — per-developer journals and session traces
- `.trellis/tasks/` — active and archived tasks (PRDs, research, jsonl context)

If a Trellis command is available on your platform (e.g. `/trellis:finish-work`, `/trellis:continue`), prefer it over manual steps. Not every platform exposes every command.

If you're using Codex or another agent-capable tool, additional project-scoped helpers may live in:
- `.agents/skills/` — reusable Trellis skills
- `.codex/agents/` — optional custom subagents

Managed by Trellis. Edits outside this block are preserved; edits inside may be overwritten by a future `trellis update`.

<!-- TRELLIS:END -->

## 当前开发基线

- 唯一产品实现是 `cli/` 下的 TypeScript、Pi SDK 与 TUI 版本；根目录命令只服务新版。
- 用户已明确删除旧产品代码、旧书稿、数据库和本地凭证，不恢复旧 Web/Python 实现，不新增旧版兼容或迁移。
- `.trellis/` 内历史记录仅作历史证据，不得据此恢复旧架构约束；当前任务方案和新版代码是开发依据。

## 项目提问原则

- 针对本项目提出每个产品、交互或技术方案问题前，先检查参考仓库
  `E:/project/ainovel-cli` 的对应实现、测试及相关文档，明确它实际如何处理该问题。
- 提问时先说明参考项目的做法及源码依据，再指出本项目需要用户决定的差异、范围或取舍；
  不脱离参考实现提出抽象选择题，不把能从参考代码回答的问题交给用户。
- 用户已明确“按 ainovel-cli 参考实现处理”的功能，按已核实的相关行为记录并推进，
  不再把它拆成参考实现已回答的小问题反复确认。只有真实差异、缺失能力或冲突才继续提问。
- 区分参考项目事实、助手建议和用户确认。参考实现不能覆盖用户明确决定，也不代表
  其所有功能必须纳入本项目；Pi SDK 适配等差异要先研究，再说明具体影响。
- 本次方案从当前对话重新建立。先前未经用户审阅的 NovelPilot 产品和设计文档，
  不得作为新方案的需求约束或授权依据。

## 功能取舍与面试价值

- 讨论一个功能是否纳入时，同时评估使用价值、技术含量、面试表达与问答适配程度，以及
  实现/验证成本。技术复杂、实现细节多，不自动等于面试或简历价值高。
- 优先判断能否用一两句话引出通用技术问题，让不了解本项目的面试官自然追问设计动机、
  替代方案、边界、权限、失败处理和验证。若需要先解释大量小说领域概念、内部状态或调用链，
  应降低其作为独立简历亮点的优先级，而不是给细节换上通用技术名词就算有价值。
- 先看 ainovel-cli 的真实实现，再用通用问题表达；具体工具名、文件、字段、状态和失败窗口
  放在证据或深入追问中，不作为项目介绍的前置知识。面试官不需要先学会我们的系统。
- 用户给出的正例是角色专属 Tool：无需介绍每个小说工具，就能讨论为何区分工具集、如何
  划分粒度、为何不用万能工具，以及执行时怎样校验权限。其他功能也应达到类似的问答可进入性。
- 判断相对主线是否新增了值得聊的通用问题，还是仅多了同类实现细节；将“适合独立写进
  简历”“适合作为已有亮点的案例”“主要是使用便利”分开评价。不要按能罗列多少术语计分，
  也不要为增加考点制造没有实际需要的复杂度。
- 给出形成可信证据的办法，例如关键行为测试、故障注入、调用量/耗时对比和真实模型样例。
  未测指标写明待测；参考项目的实现与成绩不能直接当作本项目的个人成果。
- 每次评估先给通用面试主题、少量自然追问、理解成本和相对已有能力的增量，再给证据、成本
  与建议。用户决定产品范围；确认有使用需求不等于已选定首版范围，也不应反复询问是否有需求。
- 按功能逐项讨论：每次只处理一个功能，先说明 ainovel-cli 的现有行为，再给面试表达价值、
  实现成本和建议，最后只提出一个范围决策问题；收到用户答案后更新需求，再进入下一项。
