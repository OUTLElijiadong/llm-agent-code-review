# 圆桌讨论 WebSocket 线上修复 - Final

## 交付结果

已修复并部署腾讯云线上 Agent 圆桌讨论 WebSocket 连接链路。

## 关键变更

- 前端讨论预检后把后端返回的 `ws_url` 传递到 Agent 中心与讨论面板，避免生产环境硬猜连接路径。
- WebSocket 鉴权改为优先使用 `Sec-WebSocket-Protocol: prism-auth`，避免新前端把 JWT 放在 URL query 中；后端仍兼容旧 query token。
- Caddy 显式代理 `/api/ws/*`、`/api/agents/events*`、`/healthz`、`/docs`、`/redoc`、`/openapi.json`。
- 后端 WebSocket 入口新增不含 token 的连接、拒绝和断开日志。

## 线上验证

- `http://81.70.251.90/healthz`：`200`
- `http://81.70.251.90/docs`：`200`
- `http://81.70.251.90/api/ws/discuss/probe_subprotocol`：`101 Switching Protocols`
- `wss://lijiadong.cn/api/ws/discuss/probe_wss_subprotocol` 服务器侧握手：`101 Switching Protocols`
- `http://81.70.251.90/api/agents/events?replay=0`：`200 text/event-stream`，首帧 `:connected`
- `http://81.70.251.90/api/discuss/start`：`200`，返回合法 `session_id`、`ws_url` 和 4 个参会 Agent
- 核心 API 冒烟：19 条 `200`，失败数 `0`
- 服务器侧 `https://lijiadong.cn/healthz`、`https://lijiadong.cn/docs`：`200`
- 公网端口：`80/443` 连通；后端 `8000` 服务器本机健康检查 `200`，外部业务 API 通过 Caddy 网关访问。
- Compose 状态：`cr_backend`、`cr_frontend`、`cr_mysql` 均 Running，MySQL healthy。
