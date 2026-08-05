# DESIGN：生产 Agent 审批失败回退与 tool_choice 修复

## 1. 架构图（相关链路）

```mermaid
flowchart LR
  U[管理 Agent / 成员 Agent 前端] -->|SSE resume approve/reject/answer/retry| API[api/v1/agent_responses.py]
  API --> SVC[AgentResponsesService.resume]
  SVC --> RT[ResponsesRuntime]
  RT --> STORE[DatabaseCheckpointStore/agent_response_run]
  RT --> TR[NativeResponsesTransport]
  TR -->|HTTP/SSE| DS[DeepSeek Responses]
  RT -.完成守卫证据校验.-> GUARD[completion_guard]
  GUARD -.non_thinking_repair.-> TOOLOPT[_tool_request_options]
  TOOLOPT -.原缺陷: thinking+tool_choice 400.-> FAIL[failed]
  TOOLOPT -.修复后: 省略 tool_choice.-> OK[继续工具循环]
  RT -.失败运行.-> RETRY[retry/幂等续跑回退]
```

## 2. 状态机变更

- 可恢复终态：`failed`、`incomplete`、`max_rounds_exceeded`。
- `retry`：可恢复终态 → 有 pending 回 `waiting_approval/waiting_input`；无 pending → `running` 并 `_drive`。
- `approve/reject/answer`：遇到可恢复终态且调用已有证据 → 幂等续跑（不改写已审批记录）。

## 3. 接口契约

- `POST /api/agent-responses/stream` body.action 增加 `"retry"`（与 start/approve/reject/answer 并列）。
- `retry` 请求：`{action:"retry", surface, session_id, run_id}`。
- 返回：与 start 相同的 SSE 事件流（response.created → … → response.completed/failed/…）。

## 4. 异常处理

- 非可恢复终态（running/waiting/completed/cancelled）调用 retry → `InvalidRunStateError`，SSE 返回 error 事件。
- 可恢复终态但 pending 存在 → 回到等待决策，不自动重放写操作。
- 工具执行账本 `agent_tool_execution` 仍以 request_id 幂等，续跑不会重复写。
