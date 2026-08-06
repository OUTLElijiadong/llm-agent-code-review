# 小助手启动审查契约对齐 - 对齐文档

## 原始需求

继续完成圆桌任务遗留项：修复 `test_platform_service_handlers_render_lists_and_mutations`，确保 ChatAssistant 通过对话启动审查的测试契约与生产实现一致，并完成服务器同步验证。

## 现状与根因

- `ReviewStartIn.file_ids` 要求 1-500 个文件 ID，空列表不再允许启动审查。
- ChatAssistant 已加入“缺省时自动查询项目文件”的逻辑，但过滤条件误写为 `CodeFile.status == 1`。
- `CodeFile.status` 实际为字符串字段，合法活动状态为 `"active"`，因此生产自动查询会返回空列表。
- 旧覆盖率测试仍只传 `project_id`，FakeOrchestrator 没有数据库，因而稳定失败。
- 首次线上修复后真实聊天仍返回 502：双层 ChatPlanner 绕过 legacy handler，固定工具契约强制 `file_ids`，并直接调用 Orchestrator。
- 统一入口首次生产验收又发现 Planner 生成了 `"file_ids":"$[0].files[].id"` 动态引用；当前执行器不支持该语法，导致严格校验失败并降级到 legacy handler。

## 范围

- 把 active 文件自动选择下沉到 Orchestrator 统一入口，覆盖 legacy handler、ChatPlanner 和固定工具。
- 固定工具 `file_ids` 改为可选，允许 Planner 仅提供项目 ID。
- Planner 对 `start_review.file_ids` 的 `null` 或 `$...` 动态引用按“未提供”处理，其他非数组字符串继续拒绝。
- 规划提示词明确 `start_review` 无显式文件 ID 时直接省略 `file_ids`，不先调用 `list_code_files`。
- 更新旧测试为显式 `file_ids` 场景。
- 新增真实 SQLite 测试覆盖自动查询与 deleted 文件排除。
- 不改变 ReviewService 权限、文件归属或并发控制逻辑。
- 本地通过后仅同步 Backend、测试和文档到生产服务器。

## 验收标准

1. 显式 `file_ids` 原样传给专业审查 Agent，不查询数据库。
2. 任一调用路径缺少 `file_ids` 时，由 Orchestrator 仅查询同项目、`status="active"` 的文件并按 ID 升序启动。
3. 只有 deleted 文件或数据库不可用时返回清晰失败，不调用 start_review。
4. ChatAssistant 定向测试、Ruff、compileall 和后端全量测试全部通过。
5. 生产 Backend 重建后健康，线上对话以单步 `start_review` 直达 Planner 执行，不降级、不追问。
6. 真实审查任务保留用户指定的任务名，文件数为 1 且最终进入 `success`。
