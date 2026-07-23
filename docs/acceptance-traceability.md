# 重构验收追踪

## 验收口径

阶段 0～10 的离线门禁通过即构成重构工程验收。真实模型具有概率性和不可控 token 成本，因此四次 Grok 4.5 运行属于工程完成后的表现观测，不是成功条件。

| 能力 | 权威实现 | 主要离线证据 |
| --- | --- | --- |
| 单 SQLite、34 表、Alembic drift | `app.db` | `backend/tests/db`、`test_database_engine.py` |
| 项目内 CAS 与删除隔离 | `app.store.content`、复合 FK | `test_content.py`、`test_constraints.py` |
| Book workspace/review/approval/baseline | `app.domain.book` | `test_book_discussion.py`、`test_book_lifecycle.py` |
| Arc 滚动规划与双模式审批 | `app.domain.arc` | `test_arc_lifecycle.py`、整书 driver 参数化测试 |
| Chapter/Canon 原子提交 | `app.domain.chapter` | `test_chapter_lifecycle.py`、`test_revisions.py` |
| Pydantic AI typed/text 输出 | `app.agents` | `test_pydantic_ai_contract.py`、`backend/tests/agents` |
| 5 次 transport retry、6 请求总预算、T1 | `agents.transport/contracts`、DB check | `test_transport.py`、schema tests |
| 唯一 Run Engine、Pause/Retry/C1 | `app.runtime` | `backend/tests/runtime` |
| 任务证据与 live delta 分离 | `agents.executor`、`runtime.live` | executor/live/routing tests |
| 反馈、正式修订与跨层升级 | `domain.feedback/change_requests` | feedback/change/stale-rebase tests |
| Completion、snapshot、Markdown | `domain.completion/snapshots/export` | completion/export/snapshot tests |
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
- participatory：20 个正式 Chapter、1 次 Book 批准、10 次 Arc 批准；
- 两者都到达正式 Book completion；
- 浏览器、SSE 和真实 Provider 均不是推进条件。

## 真实观测

离线工程验收后执行固定 series：

```powershell
npm.cmd run observe:live-book-series -- --case benchmark-mother-natural-book-v1 --profile-id grok-4.5 --runs 4
```

每个 slot 只允许正常产品交互，不允许技术救援。报告保存：代码/Prompt/Profile/framework/Harness 指纹、项目 ID、模式、最终权威状态、章节/Arc/gate、全部 task attempt metadata、usage、retry/repair、类型化错误、completion identity 和导出 hash。

可能结果是 0～4 个 completed；failed 和 not_run 同样是有效观测记录。series 结束后不自动分析、不修改代码、不补跑，保留现场等待后续分析。
