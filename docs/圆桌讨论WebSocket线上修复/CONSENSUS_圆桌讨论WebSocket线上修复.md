# 圆桌讨论 WebSocket 线上修复 - Consensus

## 需求描述

修复腾讯云线上 Agent 圆桌讨论 WebSocket 连接失败问题，并验证生产入口下 HTTP API、SSE、WebSocket、Swagger/OpenAPI 和健康检查均能正确接入后端。

## 验收标准

- `http://81.70.251.90/` 返回前端页面。
- `/api/auth/login`、`/api/projects`、`/api/agents/runtime` 等核心 API 通过公网入口返回成功。
- `/api/agents/events` SSE 通过公网入口返回 `200` 并可读取事件流。
- `/api/ws/discuss/{session_id}` WebSocket 通过公网入口完成 `101 Switching Protocols`。
- `/docs`、`/openapi.json`、`/healthz` 均通过 Caddy 接入后端。
- 线上容器重建后 `cr_backend`、`cr_frontend`、`cr_mysql` 均正常运行。

## 技术方案

- 前端讨论面板新增 `wsUrl` 入参，连接工具根据 `wsUrl` 生成绝对 `ws://` 或 `wss://` URL，并安全追加 URL 编码后的 token。
- 审查启动页把 `/api/discuss/start` 返回的 `ws_url` 传给 Agent 中心，Agent 中心再传给讨论面板。
- Caddyfile 增加显式 WebSocket、SSE、健康检查和 Redoc 代理块。
- WebSocket 后端入口增加连接接受、拒绝、断开日志，便于线上排查。

## 约束

- 生产环境 `.env` 保留在服务器，不随代码同步覆盖。
- WebSocket token 只作为连接鉴权使用，日志和文档不记录 token 内容。
