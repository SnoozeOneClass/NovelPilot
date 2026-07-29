# 重构验收追踪

## 验收口径

阶段 0～10 的离线门禁通过即构成重构工程验收。真实模型具有概率性和不可控 token 成本，因此四次真实模型运行属于工程完成后的表现观测，不是成功条件。

| 能力 | 权威实现 | 主要离线证据 |
| --- | --- | --- |
| 单 SQLite、39 表、Alembic drift | `app.db` | `backend/tests/db`、`test_database_engine.py` |
| 项目内 CAS 与删除隔离 | `app.store.content`、复合 FK | `test_content.py`、`test_constraints.py` |
| Book workspace/review/approval/baseline | `app.domain.book` | `test_book_discussion.py`、`test_book_lifecycle.py` |
| Book 有序 Arc 拓扑与非最终弧确定性交接 | `domain.book/authority`、`runtime.driver` | `test_completion.py`、整书 driver 参数化测试 |
| 当前 Arc 契约、双模式审批与 formal closure | `app.domain.arc`、`domain.authority` | `test_arc_lifecycle.py`、`test_completion.py`、整书 driver 参数化测试 |
| Chapter/Canon 原子提交 | `app.domain.chapter` | `test_chapter_lifecycle.py`、`test_revisions.py` |
| Book > Arc > Chapter 直属上层审查 | `domain.change_requests/authority` | `test_change_requests.py`、`test_authority_feedback.py` |
| AR1 与单轮向下纠正 lineage | `domain.authority`、`runtime.driver` | `test_authority_feedback.py`、`test_completion.py` |
| Q1 顶端叙事修订与 evidence-only correction | `domain.chapter/feedback/authority` | `test_feedback.py`、`test_authority_feedback.py` |
| Pydantic AI typed/text 输出与双流式线协议 | `app.agents.binding/transport` | `test_pydantic_ai_contract.py`、`test_binding.py`、`test_transport.py` |
| Profile capability evidence 与无密钥快照 | `app.agents.probe`、`app.profiles` | `test_probe.py`、`test_profiles.py`、secret audit |
| 5 次 transport retry、6 请求总预算、T1 | `agents.transport/contracts/executor`、DB check | `test_transport.py`、`test_executor.py`、schema tests |
| 唯一 Run Engine、Pause/Retry/C1 | `app.runtime` | `backend/tests/runtime` |
| 任务证据与 live delta 分离 | `agents.executor`、`runtime.live` | executor/live/routing tests |
| FIFO 延迟反馈、creator wait 与正式修订 | `domain.feedback/change_requests`、`api.workspace` | feedback/change/authority/API tests |
| Arc closure、Book handoff/completion、snapshot、Markdown | `domain.authority/snapshots/export` | completion/export/snapshot tests |
| 显式 API、幂等、SSE 不驱动流程 | `api.workspace`、新 React App | API tests、frontend tests/build |
| 一致备份恢复 | `db.maintenance` | `test_maintenance.py` |
| SQLite/备份/导出/报告密钥审计 | `security.audit` | `test_secret_audit.py` |
| 旧运行路径彻底退出 | 目录删除与单一 `app.main` | `acceptance_report.py` negative probes |

完整静态 inventory 由以下命令生成；任何 `partial` 或 `missing` 都返回非零状态：

```powershell
npm.cmd run acceptance
```

## 双模式整书离线验收

`backend/tests/runtime/test_domain_driver.py` 使用 Pydantic AI `FunctionModel`，但经过正式 Task Registry、Agent Executor、execution evidence、Route、Domain Commands 和 SQLite Store，而不是绕过业务层直接塞 fixture。

- full-auto：20 个正式 Chapter、1 次 Book 批准、0 次 Arc 批准；
- participatory：20 个正式 Chapter、1 次 Book 批准、2 次 Arc 批准；
- 两者都严格运行 Book 规划的两个 Arc；Arc 1 closure 确定性交接到 Arc 2，Arc 2 作为最终弧触发正式 Book completion；
- closure 由契约与已提交事实判定，检查点只由已批准 Arc 大纲长度派生；
- 无 Arc 滚动复盘、无 Chapter-to-Book 直达变更、无按次数完成；
- 浏览器、SSE 和真实 Provider 均不是推进条件。

## 真实观测

离线工程验收后执行固定 series：

```powershell
npm.cmd run experiment:live-book
```

命令默认使用应用当前选中的 Profile；当前冻结观测使用 OpenAI Responses Profile `jemmy-gpt-5.6-terra`（模型 `gpt-5.6-terra`，base URL `https://api.jemmy.icu/v1`）。每个 slot 只允许正常产品交互，不允许技术救援。报告保存：代码/Prompt/Profile/framework/Harness 指纹、项目 ID、模式、最终权威状态、章节/Arc/closure/handoff/gate、全部 task attempt metadata、usage、retry/repair、类型化错误、completion identity 和导出 hash。

可能结果是 0～4 个 completed；failed 和 not_run 同样是有效观测记录。终端按权威状态变化播报，并在 60 秒无变化时输出心跳，不生成虚假完成百分比。`latest-series.json` 和批次内 `series.json` 标识实验是否仍在运行、意外中断或已完成采集，`active_observation` 只保存最新的非权威观察。series 结束后不自动分析、不修改代码、不补跑；Codex 只在结束后读取 aggregate、slot 报告与数据库证据进行集中分析。
