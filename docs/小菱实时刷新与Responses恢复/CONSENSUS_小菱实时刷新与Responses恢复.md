# 小菱实时刷新与 Responses 恢复 - 共识文档

## 需求与验收标准

1. 管理端和用户端在会话完成、失败、等待审批、等待输入或尚无运行时，仍定期刷新当前会话。
2. 活跃状态刷新间隔为 1 秒，非活跃状态刷新间隔为 3 秒。
3. 新增的跨会话消息自动出现在小菱悬浮窗的 Agent 调用链中。
4. `send_message` / `receive_message` 条目初始 `aria-expanded=false`，详情只有用户主动展开后显示。
5. 对持久化后中断的已完成模型工具调用，重试先真实执行缺失调用并写入配对输出，再请求模型。
6. 失败或不完整模型响应中携带的调用不得被恢复执行。
7. 重试获得新的完整轮次预算，历史工具终态证据不得重复执行。
8. 单元测试、前端测试、构建、生产 API 与真实浏览器验收全部通过。

## 技术约束

- 复用现有 `AgentResponseSession`、`agentMeshToolCalls` 和 `ResponseToolTimeline`。
- 复用 `last_response` 判断工具调用是否来自明确的 `completed` 响应。
- 以 transcript 中相同 `call_id` 的 `function_call_output` 作为已执行证据。
- 不修改 DeepSeek 凭证管理方式，密钥继续由服务器 `.env` 提供。
