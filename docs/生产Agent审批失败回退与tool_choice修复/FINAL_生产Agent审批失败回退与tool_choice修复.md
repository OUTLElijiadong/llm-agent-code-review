# FINAL：生产 Agent 审批失败回退与 tool_choice 修复

## 1. 事故结论（根因）

1. DeepSeek 思考模型（deepseek-v4-flash）拒绝任何显式 `tool_choice`。完成守卫在模型“声称写失败但无失败证据”时触发 `completion_guard_non_thinking_repair`，旧代码在该分支发送 `tool_choice: auto + thinking: disabled`，上游仍返回 HTTP 400 → 运行标记 failed。
2. 运行 failed 后再次批准/拒绝/回答被 `_require_pending` 拒绝，报“当前状态为 failed，不能执行该恢复动作”；前端“可重试”没有真实通道，形成死胡同。
3. 生产运行的旧代码还缺 `a5add32` 的省略 tool_choice 修复（该修复本身也不完整，repair 分支仍发 tool_choice）。

## 2. 修复内容

后端（3 文件）：
- `deepseek_responses_runtime.py`：`_tool_request_options` 对 thinking 模型一律不携带 tool_choice；新增 `retry()` 与 `_recover_if_applied()`（审批/拒绝/回答在失败运行且有终态证据时幂等续跑）。
- `agent_responses_service.py`：`resume` 支持 action=retry。
- `api/v1/agent_responses.py`：请求契约增加 retry。

前端（2 组件 + 1 配套）：
- `AdminCopilot.vue`、`AgentChatDrawer.vue`：failed/incomplete/max_rounds_exceeded 时显示“重试运行”按钮。
- `ResponseToolTimeline.vue` 同步为 fold-block 版本（与运行镜像一致，避免回退）。

## 3. 部署记录（生产 81.70.251.90）

- 源码备份：`/opt/prism-current/.bak-agent-retry-20260805-1735/`
- 镜像：`prism-backend:agent-retry-fix-08051735`、`prism-frontend:agent-retry-fix-08051735`
- 发布变量：`deploy/.env` BACKEND_RELEASE/FRONTEND_RELEASE 已切换；`.env.bak-20260805-1757` 备份
- 旧镜像保留：`docroot-fix-08051715`、`fold-block-4fd17fb`
- 回滚：改回旧 tag 后 `docker compose --env-file deploy/.env up -d backend frontend`
- 本地提交：`5870096`

## 4. 验证

容器 healthy、/healthz /readyz 200、nginx -t 通过、HTTPS 200、镜像内代码核验通过、run_ede09 可恢复性核验通过。
