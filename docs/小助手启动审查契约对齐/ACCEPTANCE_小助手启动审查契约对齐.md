# 小助手启动审查契约对齐 - 验收记录

## 进度

- [x] T1 失败测试
- [x] T2 查询修复
- [x] T3 定向回归
- [x] T4 全量回归
- [x] T5 生产部署

## 验收证据

- 实现前：ChatAssistant 测试 `24 passed, 1 failed`，新增自动 active 查询用例准确失败。
- 局部 handler 修复后：ChatAssistant 定向 `26 passed`，但首次线上真实 ChatPlanner 冒烟仍返回 502；未创建任务。
- 统一入口首次修复后：ChatAssistant / Orchestrator / Tool Contract / ChatPlanner 共 `83 passed`。
- 第一次最终线上冒烟：任务 `#58` 成功处理 `1/1` 文件，但日志证明 Planner 因 `"file_ids":"$[0].files[].id"` 校验失败而降级。
- 动态引用回归实现前准确失败，实现后相关四文件 `84 passed`，ChatPlanner 覆盖率 100%。
- 后端全量：`1039 passed`，零失败，总覆盖率 78%。
- Ruff：`chat_agent.py`、`chat_planner.py`、`orchestrator.py`、`tool_contracts.py` 及新增测试通过。
- compileall：四份实现和四份相关测试通过。
- `git diff --check`：通过。
- 最终生产镜像：`sha256:aead8e6ff619a70f30fdf78fb71538a5ceb3bf9d68c1e6fe98fe2f2a58743ed5`。
- 最终对话冒烟：HTTP 200、`clarify=null`、单步 `start_review`，参数为 `project_id=27`、`review_type=quick`、`task_name=chat-auto-file-e2e-final`，无 `file_ids` 占位符。
- 生产任务 `#59`：任务名完整保留，`total_files=1`、`processed_files=1`、`status=success`、`score=25`、无错误。
- 容器：`running/healthy`、`RestartCount=0`、`OOMKilled=false`；Backend `/healthz` 与 `/readyz` 均正常。
