# FINAL_域名HTTPS配置

## 交付结果

`lijiadong.cn` 的服务器侧 HTTPS 配置已完成，并已恢复为 HTTPS-only 入口。当前公网域名访问仍被 DNSPod/Tencent webblock 拦截，需要在腾讯云控制台处理备案/域名接入状态后才能从公网稳定打开。

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
- Compose 已在服务器收敛公网端口：后端 `8000` 与 MySQL `3307` 改为 `127.0.0.1` 绑定，公网 API 统一经 `443` 网关进入。
- 部署脚本和部署说明已更新为 HTTPS 域名入口，不再引导生产环境访问明文 HTTP/IP。
- 已同步并部署到 `/opt/code-review`：`cr_frontend` 监听公网 `80/443`，`cr_backend` 仅监听 `127.0.0.1:8000`，`cr_mysql` 仅监听 `127.0.0.1:3307`。
- 服务器侧验证通过：`https://lijiadong.cn/healthz` 返回 `200`，`https://lijiadong.cn/api/auth/login` 返回 `200` 且业务 `code=0`。
- 证书验证通过：Let’s Encrypt RSA 证书，`CN=lijiadong.cn`，有效期 `2026-06-11` 至 `2026-09-09`。

### 当前阻塞

- 外部公网 `http://lijiadong.cn/...` 在强制解析到 `81.70.251.90` 时仍返回 DNSPod `webblock.html?d=lijiadong.cn`。
- 外部公网 `https://lijiadong.cn/` TCP 可连通，但 TLS 握手阶段返回 `SSL_ERROR_SYSCALL`，未进入 Caddy 应用日志。
- 服务器抓包显示 HTTP Host 请求与 HTTPS ClientHello 均到达 `cr_frontend` 容器，容器 ACK 后收到对端方向 `RST`；客户端同时拿到 DNSPod webblock 或 TLS reset。
- 阻塞层级判断：域名注册/DNS 状态、服务器配置、容器、证书和登录接口均正常；剩余问题属于腾讯云/运营商链路基于 Host/SNI 的网站接入管控、ICP备案或接入备案状态。
- 本机无腾讯云 CLI 凭证，且当前 Chrome 未连接 Codex Chrome Extension，无法直接进入已登录控制台代处理备案/解除拦截。

### 控制台处理后复测

```bash
cd /opt/code-review/deploy
docker compose --env-file .env ps
curl -I https://lijiadong.cn/healthz
curl -I https://lijiadong.cn/docs
```
