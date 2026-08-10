# CONSENSUS - 小菱多智能体总调度体系

> 阶段：Align 最终共识
> 日期：2026-08-10
> 状态：已确认

## 1. 最终需求

以小菱为同一账户的统一入口，在不破坏现有 Agent、RBAC、审批、Responses 和运维架构的前提下，建立持久化 Agent Mesh：统一发现、结构化投递、收件箱读取、回执、追踪、前端展示和跨会话唤醒。

## 2. 功能契约

### 2.1 ListAgents

返回四类可寻址对象：

1. 17 个运行时 Agent。
2. 仅存在于契约/确定性服务中的服务型 Agent。
3. 当前用户可调用的已发布自定义 Agent。
4. 当前账户已向服务器注册且未归档的用户端/管理端会话。

返回项至少包含 `address/name/kind/status/capabilities/session_id/surface/last_seen_at`。不得暴露其他用户会话、密钥、内部提示词或私有消息。

### 2.2 SendMessage

- 只接受标准 JSON 信封，不接受无法验证的裸文本核心字段。
- 发送方和接收方必须是 ListAgents 可解析的精确地址。
- Agent 到 Agent 必须通过 `contracts.py` 的双向协作白名单；小菱可调度全部已登记 Agent。
- 会话到会话只允许同一 `user_id`。
- 使用 `message_id` 和 `idempotency_key` 去重。
- 状态流转：`queued -> delivered -> acknowledged -> processing -> completed|failed|expired|dead_letter`。
- 写操作、外部 MCP、运维和高风险能力仍由接收会话中的工具网关与审批机制裁决。

### 2.3 会话注册和唤醒

- 打开小菱会话后向服务器登记 `user_id/surface/session_key/title` 并周期更新心跳。
- `ListAgents` 只把近期心跳的会话标记为 online，历史会话仍可列出为 offline。
- 目标会话在线且空闲时，前端收件箱轮询取得消息并以 `mesh_message_id` 启动新的 Responses 回合。
- 目标会话忙碌时消息保持 queued/delivered，不抢占当前工具调用；运行结束后处理。
- 目标会话离线时消息持久化，下一次打开后继续处理。
- 自动回合使用服务器保存的结构化协作上下文，不伪造成用户聊天消息。

### 2.4 前端可见性

- 普通用户与管理员小菱均能看到同账户会话和消息状态。
- Agent 调用链继续默认折叠；展开后显示 Agent 对话节点、方向、状态、时间、关联 trace 和脱敏摘要。
- 不展示 reasoning、密钥、Authorization、私钥、完整工具参数或其他用户数据。

## 3. 标准消息体

```json
{
  "schema_version": "1.0",
  "message_id": "msg_<uuid>",
  "idempotency_key": "<sender scoped key>",
  "trace_id": "trc_<id>",
  "correlation_id": "<parent message id or empty>",
  "causation_id": "<cause message id or empty>",
  "sent_from": "agent:orchestrator|session:<surface>:<session_id>",
  "send_to": "agent:<code>|session:<surface>:<session_id>",
  "message_type": "task.request|task.result|task.error|status.update|coordination|notification",
  "priority": "low|normal|high|critical",
  "subject": "short machine-readable summary",
  "payload": {},
  "context": {
    "task_id": null,
    "project_id": null,
    "file_id": null,
    "run_id": ""
  },
  "artifacts": [],
  "errors": [],
  "delivery": {
    "requires_ack": true,
    "max_attempts": 3,
    "expires_at": null
  }
}
```

服务器生成并覆盖 `message_id/user_id/timestamps/status/attempt_count`，客户端不得伪造所有权字段。

## 4. 验收标准

| 编号 | 验收项 | 可测试标准 |
|---|---|---|
| AC1 | 全量纳管 | ListAgents 同时返回 runtime/service/custom/session，32 份内置契约无遗漏 |
| AC2 | 用户隔离 | A 用户无法查询、投递或 ACK B 用户会话，返回 403/404 且无数据泄漏 |
| AC3 | 严格消息 | 缺字段、未知类型、未知地址、越权协作和重复幂等键均有确定性结果 |
| AC4 | 追踪 | 每条消息可按 trace_id 恢复完整父子顺序和状态时间线 |
| AC5 | 异步容错 | busy/offline 不丢消息；ACK、失败重试、过期和 dead-letter 可复现 |
| AC6 | 自动续跑 | idle 在线目标收到消息后自动启动且不添加伪用户消息 |
| AC7 | UI | 两类小菱界面默认折叠 Agent 对话，展开可查看脱敏链路；桌面与移动端无溢出 |
| AC8 | 回归 | 后端定向和扩大测试、Ruff、迁移、前端 Vitest/ESLint/type-check/build 全通过 |
| AC9 | 生产 | 备份可校验、029->030 迁移成功、五容器健康、API/浏览器/数据库证据一致 |

## 5. 约束

- 复用现有 `contracts.py`、`Message`、Responses、AgentEventBus、工具网关和发布 Agent 搜索。
- 数据库是跨进程/跨会话消息的唯一事实源；Redis 只做可选通知加速，不能成为唯一消息存储。
- 所有 API 按当前登录用户确定所有权，不能接受请求体中的 `user_id`。
- 高风险动作不得因“跨会话消息”而绕过审批。
