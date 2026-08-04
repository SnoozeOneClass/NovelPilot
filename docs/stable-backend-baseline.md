# 稳定后端基线

本页记录新仓库起点采用的脱敏工程证据，不保存原始数据库、Prompt、Context、正文、
API key 或诊断附件。

## 四轮整书真实观测

- Series：`20260803T060511Z-6b95c025`
- Profile：`jemmy-gpt-5.6-terra`
- 固定顺序：`full_auto → participatory → full_auto → participatory`
- 结果：4 个 `completed`、0 个 `failed`、0 个 `not_run`
- 技术救援：0
- 累计耗时：约 24,140 秒（约 6.7 小时）
- 累计 tokens：11,410,451
- 语义修复任务：20 个，四轮分别为 3、7、5、5
- 传输重试：3 次
- 结构化输出补充请求：7 次

这组结果证明上述代码、Profile、Prompt 与 actor policy 组合在一次四轮长跑中完成了
全部整书流程。它是一组可追踪的事实证据，不是统计样本，不能外推为任意模型、任意
Provider 或未来版本都具有同样成功率。

## 证据边界

- `test:fast` 只证明局部无模型契约；
- `test:backend-real` 使用固定 `jemmy-gpt-5.4-mini` 验证 S0～S5 生产路径；
- 四轮整书观测由用户显式运行，runner 不执行 Retry、Resume、数据库修复或其他技术救援；
- 新基线的代码、迁移、Profile 规则或领域语义发生变化后，应按变化范围重新取得相应证据，
  不能沿用本页数字为新版本背书。
