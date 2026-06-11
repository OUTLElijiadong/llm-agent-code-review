# Final：态势感知实时准确刷新

## 交付内容

- 修正后端态势感知状态推导，避免历史进行中事件导致 `working` 误报。
- `runtime` 接口同步返回最新运行状态，支撑页面首次打开时恢复真实 Agent 状态。
- 前端 Agent 中心实时合并 SSE 状态，快速刷新真实统计数据。
- 正在执行或需要关注的 Agent 自动排到列表前面。
- 补充 6A 文档和运行态单元测试。

## 关键文件

- `backend/app/services/agent_service.py`
- `backend/tests/unit/services/test_agent_runtime_v2.py`
- `frontend/src/views/agent/AgentCenter.vue`
- `docs/态势感知实时准确刷新/`

## 质量评估

- 代码范围集中在 Agent 中心态势和展示排序，不影响审查、AI 调用和安全扫描业务。
- 后端新增辅助函数保持 Python 3.9 兼容。
- 前端未引入新依赖，保留现有 Vue3 + Element Plus 实现方式。
- 已完成目标测试、静态检查、编译、前端构建和基础浏览器验证。
