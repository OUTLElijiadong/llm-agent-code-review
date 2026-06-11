# FINAL_域名HTTPS配置

## 交付结果

`lijiadong.cn` 已完成 HTTPS 配置，当前可通过 `https://lijiadong.cn/` 访问代码审查平台。

## 已完成事项

- 前端容器由 nginx 切换为 Caddy。
- 新增 `frontend/Caddyfile`，统一处理静态资源、API 反代、Swagger/OpenAPI 和 HTTPS。
- `deploy/docker-compose.yml` 为前端服务新增 `443:443`。
- 新增 `caddy_data`、`caddy_config` Docker volume，用于持久化证书和 Caddy 配置。
- `deploy/.env` 增加 `http://lijiadong.cn` 与 `https://lijiadong.cn` 到 CORS 白名单。
- Caddy 已成功通过 Let’s Encrypt 签发 `lijiadong.cn` 证书。

## 验证结果

| 验证项 | 结果 |
|---|---|
| HTTP 跳转 | `http://lijiadong.cn/` 返回 301 到 HTTPS |
| HTTPS 前端 | `https://lijiadong.cn/` 返回 HTTP 200 |
| HTTPS API 代理 | `/api/agents/runtime` 可达后端 |
| HTTPS 登录 | `/api/auth/login` 返回 HTTP 200 且包含 token |
| 证书 | Let’s Encrypt，SAN 匹配 `lijiadong.cn` |
| 服务器端 DNS | `lijiadong.cn` 解析到 `81.70.251.90` |

## 访问地址

- 正式地址：`https://lijiadong.cn/`
- Swagger：`https://lijiadong.cn/docs`
- OpenAPI：`https://lijiadong.cn/openapi.json`

## 运维命令

```bash
cd /opt/code-review/deploy
docker compose --env-file .env ps
docker compose --env-file .env logs -f frontend
```

## 2026-06-12 复核状态

### 已完成

- Caddy 配置已加固为 HTTPS 强制入口：HTTP 全量 301 到 `https://lijiadong.cn`，HTTPS 开启 HSTS。
- Caddy 已限制协议为 HTTP/1.1 + HTTP/2，并使用 RSA 证书策略，降低部分客户端/网络对 ECDSA 或 HTTP/3 的兼容性风险。
- Compose 已收敛公网端口：后端 `8000` 与 MySQL `3307` 改为 `127.0.0.1` 绑定，公网 API 统一经 `443` 网关进入。
- 部署脚本和部署说明已更新为 HTTPS 域名入口，不再引导生产环境访问明文 HTTP/IP。

### 当前阻塞

- 最新配置尚未同步到服务器：当前 SSH 到 `81.70.251.90:22` 能建立 TCP，但卡在 banner 阶段，`rsync` 上传失败。
- 外部公网访问 `https://lijiadong.cn/` 仍出现 reset/timeout；服务器本机 HTTPS 与登录接口曾返回 `200`，抓包显示更像腾讯云边界策略、实例网络或域名接入策略问题。

### 恢复后执行

```bash
cd /opt/code-review/deploy
docker compose --env-file .env up -d --no-build
docker compose --env-file .env ps
```
