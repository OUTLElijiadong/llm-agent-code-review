# Acceptance：态势感知实时准确刷新

## 完成情况

- [x] 后端 `get_situation` 改为按每个 Agent 最新生命周期事件推导状态。
- [x] 后端 `get_runtime_agents` 回填 EventBus 最新运行状态，页面首次加载不再全部固定为 idle。
- [x] 完成/失败事件可覆盖旧的进行中事件，避免完成后的 Agent 被误算为 working。
- [x] 前端 SSE 到达后即时同步 Agent 卡片状态和态势工作中/空闲计数。
- [x] 前端统计刷新防抖缩短到 800ms，兜底刷新缩短到 15s。
- [x] Agent 卡片按 `working/thinking/blocked/error/idle/offline` 状态优先展示，同状态保留注册顺序。
- [x] 后端测试新增 3 个运行态覆盖用例。

## 验证记录

- [x] `backend/.venv/bin/python -m pytest tests/unit/services/test_agent_runtime_v2.py tests/unit/services/test_agent_service.py`：13 passed。
- [x] `backend/.venv/bin/python -m ruff check app/services/agent_service.py tests/unit/services/test_agent_runtime_v2.py`：All checks passed。
- [x] `backend/.venv/bin/python -m compileall app/services/agent_service.py tests/unit/services/test_agent_runtime_v2.py`：通过。
- [x] `npm run build`：通过，保留既有 Sass/Rollup 警告。
- [x] 浏览器验证 `http://127.0.0.1:5174/login`：页面正常渲染，控制台无 error。

## 验收结论

本次需求已完成。后端态势准确性通过单元测试覆盖，前端实时刷新和排序通过类型检查、构建和基础浏览器渲染验证。
