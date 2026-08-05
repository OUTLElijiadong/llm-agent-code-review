# ALIGNMENT：生产 Agent 审批失败回退与 tool_choice 修复

> 生成时间：2026-08-05（服务器时区 Asia/Taipei）。本文档为取证结论，最终以代码、运行时和生产验证为准。

## 1. 原始需求（用户原话）

1. 生产服务器有报错：运行 `run_ede09ae1d85a49d3969cdfaadd22dca2` 当前状态为 failed，不能执行该恢复动作；管理 agent 在批准内容的时候报错。
2. 要求：在管理员的管理 agent 和普通成员的聊天 agent 做好**回退策略**。
3. 要求：**修复为什么会报错**。
4. 服务器：`81.70.251.90`，SSH 密钥 `gpt.pem`。

## 2. 现场取证（生产 81.70.251.90）

### 2.1 运行状态

`agent_response_run` 表：

| run_id | user | surface | status | version | 关键 error |
| --- | --- | --- | --- | --- | --- |
| run_ede09ae1d85a49d3969cdfaadd22dca2 | 1(admin) | admin | failed | 100 | `Responses transport 调用失败: Responses 上游 HTTP 400: Thinking mode does not support this tool_choice` |
| run_37351cfa60c44cb9b15caff1c62debf7 | 1(admin) | admin | failed | 29 | 同上 400 |
| run_9d23b43c7c8d459996771492f0ba2e33 | 1(admin) | admin | failed | 5 | 同上 400 |
| run_3ba6591671a546c293e7a81fad9d0f68 | 1(admin) | admin | failed | 13 | 同上 400 |
| run_4cf8bd787b0547d1889185aa41896375 | 1(admin) | admin | failed | 13 | 同上 400 |
| run_17ff617095aa4d898f768cdab92ab651 | 5(user) | user | failed | 20 | 上游 503 Service is too busy（另一类，已有重试逻辑但生产未部署） |
| run_fb45e1709f6244d0ade90ba81617290d 等 | 1(admin) | admin | failed | — | 完成守卫证据校验失败（“管理写请求在没有精确工具执行证据时就结束了”） |

### 2.2 根因链（已用检查点 JSON 证实）

1. 管理员在管理 agent 中批准内容（本案例是 `observability.alerts.resolve`，共约 23 条告警，approval_item 122–127+ 全部 approved，`agent_tool_execution` 全部 success）。
2. 工具执行成功后，runtime 继续模型循环；DeepSeek 思考模型（`deepseek-v4-flash`）在**不带证据的文本收尾**或“声称失败但无失败证据”时，触发完成守卫（`_admin_completion_guard`），写入 `completion_guard_non_thinking_repair=true`。
3. 下一轮 `_tool_request_options()` 对 `non_thinking_repair` 分支返回 `{"tool_choice": "auto", "thinking": {"type": "disabled"}}`；**DeepSeek 思考模式拒绝任何显式 tool_choice**（auto/required 均 400，即使带 thinking=disabled），于是上游返回 HTTP 400，runtime 将检查点标记为 `failed`。
4. 运行已 `failed` 后，用户再次点“批准/拒绝/回答”走 `resume`，`_require_pending()` 发现状态不是 `waiting_approval/waiting_input`，抛出 `InvalidRunStateError: 运行 … 当前状态为 failed，不能执行该恢复动作`——即用户看到的报错。
5. 前端两个组件（AdminCopilot、AgentChatDrawer）在审批失败后只把状态退回 pending 并提示“审批续跑失败，可重试”，但**没有真正的重试通道**，重试仍会撞上同一个 InvalidRunStateError，形成死胡同。

### 2.3 代码现状

- 生产容器/`/opt/prism-current` 运行的是**旧版** `deepseek_responses_runtime.py`（`_tool_request_options` 未含 a5add32 的“省略 tool_choice”逻辑；`_tool_choice_for_round` 仍用 `["tool_choice"]` 索引）。
- 本地 main 已含 `a5add32 fix(responses): omit tool choice in thinking mode`，但**修复不完整**：`non_thinking_repair` 分支仍发送 `tool_choice: auto`，生产观察到的正是该分支 400。
- 生产容器 `agent_responses_service.py` 缺 `resume(action="retry")` 等新能力；前端两组件无失败运行重试入口。

## 3. 需求理解与边界确认

- 范围：只修“Responses 运行时 tool_choice/thinking 400”与“失败运行恢复/回退策略（管理 agent + 成员 agent）”。
- 不做：不重构审批流、不改变权限模型、不部署与本次无关的本地未发布功能（知识库 RAG、503 重试等不属于本次必须项；但后端文件若与服务器版本差异过大，仅同步受影响的函数而非整文件覆盖，除非文件本身必须整体同步）。
- 验收标准：
  1. 修复后 `_tool_request_options("deepseek-v4-*", …, non_thinking_repair=True)` 不再携带 `tool_choice`，单测覆盖。
  2. 失败/未完成/超轮数运行可通过 `retry` 恢复；已应用审批的失败运行再次“批准/拒绝/回答”可幂等续跑而非报错。
  3. 管理 agent 与成员 agent 在运行失败时显示“重试运行”入口。
  4. 生产部署后：run 不再因 400 变 failed；对历史 failed run 可 retry；后端无 `不能执行该恢复动作` 阻断。
- 部署方式：生产无 git、无 node_modules、镜像为服务器手工构建自定义 tag。采用“源码备份 → 同步受影响文件 → 重建镜像 → 滚动重启 → 验证”的热修复路径，保留旧镜像可回滚。

## 4. 疑问澄清（已通过代码/数据自行回答）

| 问题 | 结论 |
| --- | --- |
| 为什么 run 会 failed？ | 完成守卫触发 non_thinking_repair 后，thinking 模式 + tool_choice 400（检查点 error 为证）。 |
| 为什么审批后仍报“不能执行该恢复动作”？ | 审批工具已成功、运行在后续轮次 failed；再次 resume 被 `_require_pending` 严格拒绝，且无重试通道。 |
| 管理员与成员 agent 共用实现？ | 是，`AgentResponsesService`/`ResponsesRuntime` 按 surface 复用；回退策略一处实现两端生效，前端各加入口。 |
| 是否需要前端改动？ | 是。审批失败后前端“可重试”是假的；需真实重试按钮（后端新增 retry action + 前端入口）。 |
