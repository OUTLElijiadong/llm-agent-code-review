# CONSENSUS_域名HTTPS配置

## 需求描述

用户通过 `https://lijiadong.cn` 访问代码审查平台，HTTP 请求自动跳转到 HTTPS。

## 验收标准

- HTTP 仅作为跳转入口，`http://*` 必须 301 到 `https://lijiadong.cn{uri}`。
- Docker Compose 暴露 `80` 和 `443`。
- `lijiadong.cn` DNS A 记录指向 `81.70.251.90` 后，`https://lijiadong.cn` 能正常访问。
- `/api/*`、`/api/ws/*`、`/docs`、`/openapi.json` 在域名下仍能正确反代到后端。
- 后端 `8000` 与 MySQL `3307` 不作为公网业务入口，仅允许服务器本机/容器网络访问。

## 技术方案

- 前端镜像最终运行阶段切换为 `caddy:2-alpine`。
- 新增 `frontend/Caddyfile`：
  - 静态 SPA 文件服务；
  - API、WebSocket、Swagger/OpenAPI 反向代理；
  - `lijiadong.cn` 自动 HTTPS；
  - HTTP 全量重定向到 HTTPS；
  - 仅启用 HTTP/1.1 + HTTP/2，并使用 RSA 证书策略兼容更多客户端。
- Compose 为前端服务增加 `443:443` 和 Caddy 数据卷，并将后端/数据库宿主机端口绑定到 `127.0.0.1`。

## DNS 记录

腾讯云 DNS 解析中需要保持以下记录：

| 主机记录 | 类型 | 记录值 |
|---|---|---|
| `@` | A | `81.70.251.90` |
