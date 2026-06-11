# 圆桌讨论 WebSocket 线上修复 - Acceptance

| 检查项 | 状态 | 说明 |
| --- | --- | --- |
| 线上问题定位 | 已完成 | 预检 API 到达后端，WebSocket 后端和 Caddy 服务器侧握手均可返回 `101` |
| 前端连接 URL 修复 | 已完成 | 讨论预检返回的 `ws_url` 已通过 `discuss_ws` 传递到讨论面板 |
| Caddy 显式代理 | 已完成 | 容器内 `caddy validate --config /etc/caddy/Caddyfile` 通过 |
| 后端 WebSocket 日志 | 已完成 | 线上日志显示 `[WS] 讨论连接已接受 session=probe_subprotocol pending=False` |
| 前端构建 | 已完成 | 本机和服务器 Docker 构建均完成，`npm run build` 通过 |
| 线上 HTTP API 验证 | 已完成 | 19 个核心 GET API 通过公网 Caddy 入口返回 `200`，失败数 `0` |
| 线上讨论预检验证 | 已完成 | `/api/discuss/start` 返回 `200`，`session_id` 为 `disc_` 前缀，`ws_url` 为 `/api/ws/discuss/*`，参会 Agent 为 4 个 |
| 线上 SSE 验证 | 已完成 | `/api/agents/events?replay=0` 返回 `200 text/event-stream`，首帧 `:connected` |
| 线上 WebSocket 验证 | 已完成 | HTTP/IP 与服务器侧 WSS 域名入口使用 `Sec-WebSocket-Protocol: prism-auth` 均返回 `101 Switching Protocols` |
| 线上 HTTPS 验证 | 已完成 | 服务器侧 `https://lijiadong.cn/healthz` 与 `/docs` 均返回 `200` |
| 线上端口验证 | 已完成 | 公网 `80/443` 可连通；后端 `8000` 在服务器本机返回 `200`，业务 API 统一通过 Caddy 网关接入 |
