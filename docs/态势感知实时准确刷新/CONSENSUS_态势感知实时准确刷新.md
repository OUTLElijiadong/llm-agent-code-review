# Consensus：态势感知实时准确刷新

## 需求描述

Agent 中心的态势感知需要实时、准确反映当前 Agent 在岗、工作中、空闲、今日调用、近 60 分钟波形和热点 Agent。正在执行工作的 Agent 需要在列表中前置，方便用户优先观察当前活跃执行者。

## 验收标准

- 后端 `get_situation` 在 Agent 完成或失败后不再把该 Agent 误计入 `working`。
- 后端按最新事件推导运行状态，历史进行中事件不会覆盖更新的完成事件。
- 前端收到 `dispatch/thinking/progress/complete/failed/clarify` SSE 后即时更新卡片状态和态势计数。
- 前端 Agent 列表中 `working/thinking/blocked/error` 状态 Agent 排在 `idle/offline` 前面，同状态保持注册顺序。
- 后端单元测试通过，前端类型检查和构建通过。

## 技术方案

- 在 `agent_service.py` 新增事件状态推导辅助函数，按 `timestamp` 为每个 Agent 选最新事件。
- `get_situation` 复用状态推导结果计算 `working/idle`。
- 在 `AgentCenter.vue` 中新增状态优先级排序 computed，替代直接使用注册顺序。
- SSE 事件到达后立即刷新态势计数，并对完成/失败/追问事件触发短防抖接口刷新。

## 约束

- 不引入新的前后端依赖。
- 不变更 API 路由路径和响应字段结构。
- 不覆盖工作区已有前端视觉优化改动。
