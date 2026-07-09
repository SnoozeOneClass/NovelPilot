# Novelpilot 文档

这个目录保存理解 Novelpilot 所需的最小公开文档集，同时不会暴露本地 Trellis 或 agent 工作区文件。

Novelpilot 是一个本地、单用户的长篇 AI 小说创作工作台。它的主要工程思想是三层 Agent Loop Harness：LLM 负责语义工作，harness 负责控制上下文、产物、验证、路由和已提交状态。

## 阅读顺序

1. [架构说明](./architecture.md)
   说明产品目标、三层 loop、候选内容与已提交内容的边界、存储模型、运行控制和完成证据。

2. [本地使用](./local-usage.md)
   说明如何在本地运行应用、配置 LLM profile、新建小说项目、启动 harness、导出全书，以及运行验证命令。

根目录的 [README](../README.md) 仍然是快速开始入口。本目录下的文档是公开版的精简说明，用来替代只保存在本地 Trellis 文件中的更深入规划记录。

