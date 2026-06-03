# CONSENSUS_多Agent优化

## 1. 需求描述

在现有代码审查平台中加入多 agent 审查概念,修正已发现的前后端和文档不一致问题,并补充必要测试与项目文档。

## 2. 实现方案

- 新增 `app/ai/multi_agent.py`,定义通用、安全、性能、可靠性、可维护性五类代理画像。
- `ReviewService` 根据 `review_type` 选择代理组合:
  - `quick/standard`: 通用质量代理
  - `security`: 安全审查代理 + 可靠性代理
  - `performance`: 性能审查代理 + 可维护性代理
  - `full`: 安全、可靠性、性能、可维护性四代理协同
- Prompt 增加“本轮审查代理”段落,每次调用聚焦不同问题类型。
- 服务层对同一文件、同一行、同类问题做去重。
- 修正分片行号规则:模型返回相对行号,后端转为原文件绝对行号。
- 前端审查类型、任务状态、跳转路径与后端对齐。

## 3. 技术约束

- 不新增数据库迁移,继续使用 `review_task`、`review_issue`、`ai_call_log`。
- DeepSeek API Key 仍只从 `.env` 读取,不进入前端和文档示例真实值。
- 保持同步审查模式,多 agent 会增加 DeepSeek 调用次数,因此只在专项和全面审查启用多代理。

## 4. 验收标准

- `full` 审查能通过多代理组合生成 Prompt。
- `security/performance` 能映射到专项代理组合。
- `standard` 保持原模型标签 `deepseek-chat`。
- 新增测试通过。
- 前端构建通过。
- 根说明文档和 docs 关键文档口径一致。

