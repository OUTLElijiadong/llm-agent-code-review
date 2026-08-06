# 小助手启动审查契约对齐 - 共识文档

## 明确需求

统一 `Orchestrator.start_review` 允许两种输入：调用方明确提供非空 `file_ids` 时直接使用；仅提供 `project_id` 时，从请求级数据库会话查询该项目全部 active 文件。ChatAssistant legacy handler、ChatPlanner 固定工具和其他调用方必须共享此入口。

## 技术方案

- `StartReviewArguments.file_ids` 使用空列表默认值，Planner Schema 只要求 `project_id`。
- Orchestrator 使用 `CodeFile.status == "active"` 并按 ID 升序解析缺省文件。
- ChatAssistant 删除重复数据库查询，只转发显式或空文件列表。
- ChatPlanner 只对 `start_review.file_ids` 的 `null` 或 `$...` 模型动态引用做省略处理；其他错误类型仍由 Pydantic 严格拒绝。
- Planner prompt 禁止 `$...` 引用语法，并明确只有项目 ID 时直接调用 `start_review`。
- 为 ChatAssistant 和 Orchestrator 入口补齐函数级契约注释。
- 旧综合 handler 测试显式传文件 ID；Orchestrator SQLite 测试验证自动查询、无可用文件、无 DB 和查询异常。

## 边界与约束

- 文件归属和项目权限仍由 `review_service.start` 再次校验。
- 不自动包含 deleted 文件，不创建占位文件，不放宽 `ReviewStartIn`。
- 不实现通用调用链占位符解析器，仅利用 `start_review` 已有的服务端文件解析能力。
- 不引入数据库迁移和新依赖。
