# Align：态势感知实时准确刷新

## 原始需求

用户希望「态势感知的数据是实时刷新且准确的」，并要求「把正在执行工作的 Agent 放到调整顺序前面去」。

## 项目上下文

- 后端为 FastAPI + SQLAlchemy，Agent 中心接口位于 `backend/app/api/v1/agents.py`。
- 态势感知聚合位于 `backend/app/services/agent_service.py#get_situation`。
- Agent 实时生命周期事件来自 `AgentEventBus`，前端通过 `frontend/src/utils/agentEventStream.ts` 订阅 SSE。
- Agent 中心页面位于 `frontend/src/views/agent/AgentCenter.vue`，态势展示组件为 `frontend/src/components/agent/SituationPanel.vue`。

## 需求边界

- 修正态势感知 `online/working/idle/today_calls/spectrum/hotspots` 的准确性。
- Agent 事件到达后前端需要即时更新可见状态和态势统计，并通过轻量接口刷新真实调用统计。
- 正在执行中的 Agent 在 Agent 卡片列表中排在空闲 Agent 前面，同状态内保持原注册顺序。
- 不改动 Agent 注册、AI 调用、审查任务执行和安全扫描业务逻辑。

## 已识别问题

- 后端当前用最近 60 秒内出现过 `dispatch/thinking/progress` 判断 working，但没有用 `complete/failed/clarify` 终止状态，因此完成后的 Agent 会被短时间误算为工作中。
- 前端在 SSE 事件到达时只局部修改 `working/idle`，完成事件后的统计刷新存在延迟，态势面板不够实时。
- Agent 卡片列表当前按注册顺序展示，工作中的 Agent 不会自动前置。

## 决策

- 后端按每个 Agent 的最新生命周期事件推导状态，只有最新事件仍为 `dispatch/thinking/progress` 且未超过活动窗口时才计入 `working`。
- 前端对事件先即时合并状态和计数，再通过短防抖刷新真实统计与态势数据。
- Agent 列表排序使用状态优先级，不改变注册中心返回的基础顺序。
