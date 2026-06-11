# ACCEPTANCE_全量项目安全扫描

## 验收记录

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| 后端全量扫描 API | 已完成 | `POST /api/security/scan-all-projects` 已在 OpenAPI 注册 |
| 单项目扫描保留 | 已完成 | `POST /api/security/scan-project` 未改动，项目详情页入口保留 |
| 接口扫描 | 已完成 | 项目级扫描可输出 `api_endpoints`，覆盖常见 Python/Node/Java/Django 路由和前端 HTTP client 调用 |
| 代码联动性 | 已完成 | 威胁模型可输出 `code_links`，连接接口、危险接收点和跨文件数据流 |
| 多 Agent 讨论 | 已完成 | 全量扫描返回 `discussion`，包含多 Agent 发言、共识和行动项 |
| 安全中心全量扫描入口 | 已完成 | `SecurityCenter.vue` 打开 `SecurityScanModal source=all-projects` |
| 后端测试 | 已通过 | `backend/.venv/bin/python -m pytest backend/tests/unit/services/test_security_sentinel_agent.py`，21 passed |
| 后端编译 | 已通过 | `compileall` 覆盖本次后端变更文件 |
| 后端 Ruff | 已通过 | `ruff check` 覆盖本次后端变更文件 |
| OpenAPI 契约 | 已通过 | `/api/security/scan-all-projects` 已注册，`discussion`、`api_endpoints`、`code_links` 已出现在 schema 中 |
| 前端类型检查/构建 | 已通过 | `cd frontend && npm run build` 通过，包含 `vue-tsc` |
| 前端轻量服务检查 | 已完成 | Vite `/security` 返回 HTTP 200；当前环境未安装 Playwright，未做登录后浏览器交互 |

## 待记录问题

- 未执行真实 DeepSeek 安全扫描，避免在开发验证中触发外部模型调用和消耗 API 配额。
- 未启动后端和 MySQL 做登录后实点，需在完整环境中补一次安全中心按钮点击验证。
- 未执行 Playwright 浏览器交互检查；本机前端依赖中未安装 Playwright。
