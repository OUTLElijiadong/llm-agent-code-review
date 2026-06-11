# CONSENSUS_域名HTTPS配置

## 需求描述

用户通过 `https://lijiadong.cn` 访问代码审查平台，HTTP 请求自动跳转到 HTTPS。

## 验收标准

- `http://81.70.251.90/` 保持可访问。
- Docker Compose 暴露 `80` 和 `443`。
- `lijiadong.cn` DNS A 记录指向 `81.70.251.90` 后，`https://lijiadong.cn` 能正常访问。
- `/api/*`、`/api/ws/*`、`/docs`、`/openapi.json` 在域名下仍能正确反代到后端。

## 技术方案

- 前端镜像最终运行阶段切换为 `caddy:2-alpine`。
- 新增 `frontend/Caddyfile`：
  - 静态 SPA 文件服务；
  - API、WebSocket、Swagger/OpenAPI 反向代理；
  - `lijiadong.cn` 自动 HTTPS；
  - HTTP 根域重定向到 HTTPS。
- Compose 为前端服务增加 `443:443` 和 Caddy 数据卷。

## DNS 记录

腾讯云 DNS 解析中需要保持以下记录：

| 主机记录 | 类型 | 记录值 |
|---|---|---|
| `@` | A | `81.70.251.90` |
