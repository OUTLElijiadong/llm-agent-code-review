# 小助手启动审查契约对齐 - 最终总结

## 本地结果

- `file_ids` 在 Planner 工具契约中改为可选，模型只给项目 ID即可执行。
- 缺省文件解析统一下沉到 Orchestrator，覆盖 ChatAssistant 和 ChatPlanner 两条路径。
- 自动查询按 `CodeFile.id` 升序，只选择当前项目 `status="active"` 文件。
- 显式 `file_ids` 不查库并原样传递给专业审查 Agent。
- deleted 文件、其他项目文件和查询异常不会进入审查任务。
- Planner 生成的 `null` 或 `$...` 动态文件引用会转为服务端 active 文件解析，其他错误字符串仍严格拒绝。
- Planner prompt 明确只有项目 ID 时使用单步 `start_review`，不再生成无法执行的跨步占位符。
- 原全量唯一失败已消除；相关四文件 `84 passed`，后端全量 `1039 passed`。

## 生产结果

- 白名单源码、测试和文档已同步到 `81.70.251.90:/opt/code-review`，未覆盖其他服务器改动。
- 最终 Backend 镜像为 `sha256:aead8e6ff619a70f30fdf78fb71538a5ceb3bf9d68c1e6fe98fe2f2a58743ed5`。
- 真实 `/api/ai/chat` 请求返回 HTTP 200，不追问、不降级，`plan_steps` 仅有一步 `start_review`。
- 生产任务 `#59` 保留任务名 `chat-auto-file-e2e-final`，自动选择 1 个 active 文件并成功处理。
- Backend 保持 `healthy`，重启次数 0，未发生 OOM，`/healthz` 与 `/readyz` 正常。

## 备份

- 源码与数据库基线备份位于 `/opt/code-review/backups/chat_review_20260715_141746/`。
- Planner 动态引用修复前的两份文件备份为 `source_planner_reference_before.tar.gz`。
