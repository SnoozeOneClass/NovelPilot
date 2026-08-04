# 后端验收与证据追踪

NovelPilot 按证据范围给出结论：Mock、手工生命周期状态和 `FunctionModel` 只验证
局部契约，不能推出“后端已经可运行”。项目级结论必须来自生产组件的真实路径。

## 三层证据

| 层级 | 目的 | 模型 | 能得出的结论 |
| --- | --- | --- | --- |
| 快速单元/契约证据 | 纯函数、Schema、SQL 约束、CAS、迁移、协议错误分类 | 通常不调用 | 某个局部契约成立 |
| 工程真实场景验收 | 生产接线、跨 Loop 交接、分层权威、恢复、真实 Provider | 固定 `jemmy-gpt-5.4-mini` | 本次目标工程场景通过 |
| 用户四次长跑 | 整书长流程和未知累积问题 | 当前用户实验 Profile | 一次事实性长流程里程碑，不是统计保证 |

四次长跑不是工程真实场景的替代品，工程场景也不冒充长篇稳定性证明。两者会经过
一部分相同生产代码，这是必要的交叉证据：前者定向复现已知失败类别，后者发现未知
长流程问题。

## 标准命令

```cmd
npm.cmd run test:fast
npm.cmd run test:backend-real
npm.cmd run acceptance
npm.cmd run experiment:live-book
npm.cmd run experiment:live-book -- --runs 2
```

- `test:fast`：在隔离临时库执行 fresh migration、Schema drift/health、downgrade/upgrade
  往返，再执行 lint、type check 和无模型局部测试；它不读取或迁移普通项目数据库，
  也不产生后端验收结论。
- `test:backend-real`：先做 S0 Profile 探测，再运行 S1～S5。
- `acceptance`：依次执行 `test:fast` 与 `test:backend-real`。
- `experiment:live-book`：仍由用户手动启动，默认保持四次长跑、当前 selected Profile
  和无技术救援边界；`--runs 2` 只执行 `full_auto → participatory` 双模式回归，
  不替代四次长跑里程碑。任何其他测试命令都不会调用它。

前端 lint、测试、类型检查与构建属于仓库完整性检查，不作为后端验收结论，也不表示前端产品设计已经完成。

## 什么是工程真实场景

一个场景必须同时满足：

1. 从隔离的空 SQLite 数据库执行生产 Alembic migration；
2. 通过 `create_app()` 和真实 FastAPI lifespan 创建唯一 Run Engine；
3. 只通过公开 HTTP API 创建项目、启动、回答 Book、审批、反馈、暂停或恢复；
4. 显式绑定 `jemmy-gpt-5.4-mini` 到 Book、Arc、Chapter、Evaluator，不修改
   `selected_profile_id`；
5. 使用生产 Pydantic AI binding、OpenAI Responses Adapter 和真实 Provider；
6. task、attempt、Context、result、delivery、review、baseline、event 全由生产代码产生；
7. 场景不得提交 Agent result、内部 task/baseline ID，或直接写数据库；
8. 数据库仅用于只读核验权威身份、来源绑定、版本、状态和终止条件；
9. 断言语义不变量和合法终态集合，不逐字匹配模型措辞；
10. 失败时保留脱敏报告与隔离数据库，不修改 Prompt、Profile、模型输出或状态后续跑。

`TestClient` 只替代 socket 传输，不替代应用组件。它仍会启动/关闭 lifespan，
Run Engine 在真实异步循环运行，Provider 请求仍是外部 HTTP。

## S0～S5 场景矩阵

| 场景 | 真实输入/检查点 | 主要不变量 |
| --- | --- | --- |
| S0 Provider contract | 对 5.4-mini 执行生产 capability probe | structured output、text stream、usage 和 fingerprint 一致 |
| S1 基础纵切 | Book discussion → Book 审批 → Arc 规划/审批 → Chapter plan/draft/observe/evaluate/commit | producer、持久化和 consumer 都成功；任务绑定具体 baseline |
| S2 开放世界证据 | 前文沉默，当前章首次建立普通事实 | 沉默不是 `explicit_conflict`；不为证明“过去没发生”而改写历史 |
| S3 派生证据权威 | 正文明确建立事实，Observation/Canon 从正文派生 | prose 是叙事来源；若发生 evidence-only repair，必须保持同一 prose ref |
| S4 分层语义压力 | Chapter 反馈与 Book 正式承诺发生张力 | 同一反馈依次进入 Chapter、Arc、Book 审查；Book 可保留当前基线并授权 Arc 纠正，或让 successor 停在人工批准；若发生 Book round-1，原请求保持不变而 task/review 必须绑定由 round-0 产生的精确 Arc successor；下层不能直接替换上层 |
| S5 持久恢复 | 第一章正式提交后安全暂停、关闭 lifespan、同库重开并继续 | current pointer、work cycle、delivery 和事件不靠内存流重建；任务不重复投递 |

任务数和最长时间只是费用/失控保护，不是小说完成条件。每层仍由自身语义终止契约
决定完成。

## 共享验收不变量

- Book、Arc、Chapter baseline 版本从 1 连续递增，successor 的 parent 指向前一版；
- 旧 baseline 行永远保留，current 指针移动不等于覆盖历史；
- 每个正式 Chapter 绑定实际使用的 Book、Arc、Canon-before/after 和 prose 来源；
- 所有真实 Agent task 使用 `jemmy-gpt-5.4-mini` 的同一配置 fingerprint；
- 同一 correction lineage 的自动轮次只能是 0 或 1；
- 成功 task 必须被 Domain 消费或明确标记 stale；只有刻意的安全暂停可以暂留
  一个待投递完成动作；
- 用户反馈通过公开 API 排队并在原子动作结束后的安全边界生效；
- 已应用的 Arc/Chapter 指导必须同时进入当前生产任务与本层 Evaluator Context；
  `chapter.observe` 例外，它只从实际正文派生，不能把“用户想要什么”记成“正文已经写了什么”；
- Evaluator 只判断；只有 Harness Domain Command 能提交正式状态；
- Provider 错误与 Harness/Domain 错误必须分开分类；
- 报告不保存 API key、完整 creator brief/feedback、Prompt、Context、正文、
  content blob 或原始诊断附件。

## 失败语义

| 类型 | 验收处理 |
| --- | --- |
| auth、quota、network、timeout、Provider capability | 失败并标记 `external_provider`；不归咎于 Harness，也不能标记通过 |
| 模型产生合法结构，但 delivery/Route/Domain 违反权威契约 | `harness_or_domain`，属于项目缺陷 |
| 模型耗尽生产重试仍不能满足声明的 typed output | 真实 Profile 适配失败 |
| 语义压力到达明确 creator decision / Book successor approval | 合法人工终态，不自动批准 |
| 系统不能合法处理 | 必须进入带 failure code 的 `failure_paused`，不能 pending 空转 |

场景 actor 没有失败 Retry 能力。失败后的数据库只用于定位第一个断裂的不变量。

## 现有测试的重新分类

| 既有形式 | 保留价值 | 当前分类 |
| --- | --- | --- |
| Schema/FK/unique/CAS/migration 测试 | 精确、快速验证数据库负约束 | 单元/契约证据 |
| `MockTransport` 错误分类 | 确定性覆盖超时、重试和协议边界 | 单元/契约证据 |
| `object.__new__(DomainRunDriver)`、`AsyncMock` 私有路由测试 | 可定位单个分支，但绕过生产装配 | `synthetic_integration`，不得宣称验收 |
| `insert_successful_task()` / `seed_approved_book_and_arc()` | 低成本构造 Domain/DB 前置状态 | 仅限局部测试，真实场景禁止导入 |
| 局部 `FunctionModel` Agent/Executor 测试 | 验证结构化输出、文本流、超时和请求预算合同 | 单元/契约证据，不是模型/后端可用性 |
| `run_engine_enabled=False` API 测试 | 请求验证、幂等和错误 envelope | 局部 API 契约 |
| S0～S5 | 生产装配、真实模型、跨层交接与权威 | 工程真实场景验收 |
| 四次整书实验 | 未知长流程稳定性 | 用户长流程里程碑 |

## 证据位置

工程场景每次写入：

```text
data/backend-real-acceptance/
  latest-run.json
  <run-id>/
    frozen-run.json
    progress.json
    aggregate.json
    base-vertical-v1.json
    open-world-evidence-v1.json
    derived-evidence-v1.json
    hierarchical-pressure-v1.json
    <case>/novelpilot.sqlite3
```

报告是数据库权威证据的脱敏索引，不成为第二套小说事实。失败数据库被保留，便于在
不重跑模型的情况下确认第一个 producer→persistence→consumer 断点。

整书观测仍写入 `data/live-observations/`，使用普通项目数据库和当前 selected
Profile；默认四次长跑与 `--runs 2` 双模式回归都不会被 `acceptance` 隐式启动或重置。

当前已接受的四轮事实摘要见 [稳定后端基线](stable-backend-baseline.md)。原始数据库、
Prompt、Context、正文与诊断附件仍只保留在 git ignored 的本地目录。
