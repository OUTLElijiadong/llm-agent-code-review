# CONSENSUS：生产 Agent 审批失败回退与 tool_choice 修复

## 1. 需求描述

- 修复生产 81.70.251.90 上管理 agent 审批后运行失败（`run_ede09ae1d85a49d3969cdfaadd22dca2` failed，报“不能执行该恢复动作”）的根因：DeepSeek thinking 模式 + 显式 tool_choice 400。
- 为管理员管理 agent 与普通成员聊天 agent 增加失败运行回退策略：失败运行可重试；已应用审批的失败运行再次决策可幂等续跑。

## 2. 技术方案

### 后端（3 文件）
1. `deepseek_responses_runtime.py`
   - `_tool_request_options`：`_model_disallows_required_tool_choice` 判定提前到最前，thinking 模型任何情况都不携带 `tool_choice`（含 non_thinking_repair 分支）。
   - `_tool_choice_for_round`：`.get("tool_choice", "auto")`（生产旧版用索引会 KeyError）。
   - 新增 `_TERMINAL_RECOVERABLE_STATUSES = {failed, incomplete, max_rounds_exceeded}`。
   - `approve/reject/answer`：若运行处于可恢复终态、`pending is None` 且该 call_id 在 transcript 已有 function_call_output 证据（说明决策副作用已执行），则重置为 running 并继续 `_drive`（幂等续跑，替代硬报错）。
   - 新增 `retry(run_id)`：仅可恢复终态可重试；有 pending 则回到 `waiting_approval/waiting_input`，无 pending 则清 error 置 running 并 `_drive`。
2. `agent_responses_service.py`：`resume()` 增加 `action == "retry"` 分支调用 `runtime.retry`。
3. `api/v1/agent_responses.py`：`AgentResponsesRequest.action` Literal 增加 `"retry"`。

### 前端（2 文件）
4. `AdminCopilot.vue`、`AgentChatDrawer.vue`：当 `sessionRun.status` 为 failed/incomplete/max_rounds_exceeded 时，标题栏显示“重试运行”按钮，调用 `runResponse({action:'retry', run_id, surface, session_id})`。

## 3. 验收标准

- [ ] 单测：thinking 模型 non_thinking_repair 不携带 tool_choice。
- [ ] 单测：failed 检查点 retry 可恢复；running 状态 retry 拒绝。
- [ ] 单测：已执行审批的 failed 运行 approve 可幂等续跑。
- [ ] 后端 pytest（相关测试文件 + 契约）通过；前端 lint/test/build 通过。
- [ ] 生产部署后健康检查通过；对 `run_ede09ae1d85a49d3969cdfaadd22dca2` 可 retry 或展示明确恢复路径；不再新增 400 failed。

## 4. 边界与限制

- 不部署本地其它未发布功能（除非文件与服务器差异使整体同步不可避免且验证通过）。
- 部署保留旧镜像 `prism-backend:docroot-fix-08051715` / `prism-frontend:fold-block-4fd17fb` 可回滚；修改文件前先备份。
- 历史 failed run 不做自动批量重试，由用户/前端手动触发 retry（避免在审批语义上自动重放写操作）。
