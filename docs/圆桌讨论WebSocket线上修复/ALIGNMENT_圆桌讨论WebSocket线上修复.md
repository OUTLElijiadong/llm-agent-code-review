# 圆桌讨论 WebSocket 线上修复 - Align

## 原始需求

用户反馈腾讯云服务器上 Agent 圆桌讨论显示 WebSocket 连接失败，要求确保圆桌讨论可连接，并确保所有 API 入口和端口成功接入。

## 项目上下文

- 前端 Vue 通过 `/api/discuss/start` 创建讨论会话，再连接 `/api/ws/discuss/{session_id}?token=<JWT>`。
- 后端 FastAPI 通过 `app.add_api_websocket_route("/api/ws/discuss/{session_id}", ws_discuss)` 提供 WebSocket。
- 生产入口为前端 Caddy 容器，负责代理 `/api/*`、`/docs`、`/openapi.json` 到后端。
- 腾讯云服务器项目目录为 `/opt/code-review`，Compose 目录为 `/opt/code-review/deploy`。

## 已确认事实

- `/api/discuss/start` 已到达后端并返回 `200`。
- 后端直连 `127.0.0.1:8000/api/ws/discuss/*` WebSocket 握手返回 `101`。
- 服务器本机通过 Caddy HTTPS 访问 `/api/ws/discuss/*` WebSocket 握手返回 `101`。
- 当前 Caddyfile 对 WebSocket 依赖通用 `/api/*` 代理规则，缺少显式 `/api/ws/*` 和 SSE 接线说明。

## 边界

- 本次只修复线上连接链路、代理配置、连接 URL 生成和验证记录。
- 不改圆桌讨论的 AI 编排策略和报告生成逻辑。
- 不输出、不提交任何 `.env`、JWT、数据库密码或 API Key。

## 关键决策

- 前端使用后端返回的 `ws_url` 作为连接路径，默认回退到 `/api/ws/discuss/{session_id}`。
- Caddy 显式代理 `/api/ws/*`、`/api/agents/events*`、`/healthz`、`/redoc`，保证 API/SSE/WebSocket 入口清晰。
- 后端新增 WebSocket 接入日志，但不记录 token。
