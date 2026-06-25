# ACCEPTANCE_域名HTTPS配置

## 验收记录

| 检查项 | 状态 | 结果 |
|---|---|---|
| DNS 解析检查 | 已完成 | 服务器端公共 DNS 均解析到 `81.70.251.90`；本机部分解析器短时可能仍有旧缓存 |
| 服务器端口检查 | 已完成 | 服务器 `443` 已由前端 Caddy 容器监听 |
| Caddy 配置 | 已完成 | 已新增 `frontend/Caddyfile` |
| Compose 配置 | 已完成 | 已新增 `443:443` 与 Caddy 数据卷 |
| 前端重建 | 已完成 | 前端 Caddy 镜像构建成功，容器已启动 |
| HTTP 验证 | 已完成 | `http://lijiadong.cn/` 返回 301 跳转到 HTTPS |
| HTTPS 验证 | 已完成 | `https://lijiadong.cn/` 返回 HTTP 200 |
| API 代理验证 | 已完成 | `https://lijiadong.cn/api/agents/runtime` 到达后端并返回认证缺失的业务错误 |
| 登录验证 | 已完成 | `https://lijiadong.cn/api/auth/login` 返回 HTTP 200 且包含 token |
| 证书验证 | 已完成 | Let’s Encrypt 证书已签发，域名匹配 `lijiadong.cn` |

## DNS 记录

| 主机记录 | 类型 | 线路 | 记录值 | TTL |
|---|---|---|---|---|
| `@` | A | 默认 | `81.70.251.90` | 600 |

## 验收结论

初次配置时 `https://lijiadong.cn` 已完成服务器侧与公网验收；2026-06-12 复核发现外部公网链路出现 reset/timeout。最新一次处理已把服务器恢复为 HTTPS-only 入口并完成部署，但公网域名仍被 DNSPod/Tencent webblock 拦截，需在腾讯云控制台处理备案/域名接入状态。

## 2026-06-12 HTTPS 强制入口复核

| 检查项 | 状态 | 结果 |
|---|---|---|
| 服务器侧 HTTPS 首页 | 已完成 | 服务器本机 `curl -I https://lijiadong.cn/` 返回 `HTTP/2 200` |
| 服务器侧 HTTPS 登录 | 已完成 | 服务器本机 `POST https://lijiadong.cn/api/auth/login` 返回 `200` |
| 证书兼容性 | 已完成 | 已重新签发 Let’s Encrypt RSA 证书，公钥算法 `rsaEncryption` |
| Caddy HTTPS 策略 | 已完成 | 本地配置已限制协议为 `h1 h2`，HTTP 全量跳 HTTPS，HTTPS 开启 HSTS |
| API 端口策略 | 已完成 | 本地与服务器 Compose 均已将 `8000`、`3307` 改为 `127.0.0.1` 绑定，公网 API 统一走 `443` |
| 本地配置校验 | 已完成 | `bash -n deploy/deploy.sh`、`cd deploy && docker compose config --quiet` 均通过 |
| 服务器同步 | 已完成 | 已同步 `frontend/Caddyfile`、前端源码和 `deploy/docker-compose.yml` 到 `/opt/code-review`，并重建/重启 `cr_frontend` |
| 服务器端口复核 | 已完成 | `cr_frontend` 暴露 `80/443`；`cr_backend` 为 `127.0.0.1:8000`；`cr_mysql` 为 `127.0.0.1:3307` |
| 服务器侧 HTTPS 登录 | 已完成 | 服务器本机 `POST https://lijiadong.cn/api/auth/login` 返回 `200`，业务 `code=0` |
| IP 明文访问策略 | 已完成 | 公网 `http://81.70.251.90/...` 由 Caddy 返回 `301` 到 `https://lijiadong.cn/...` |
| 外部公网 HTTP Host | 阻塞 | 本机强制解析 `lijiadong.cn:80 -> 81.70.251.90` 时返回 `https://dnspod.qcloud.com/static/webblock.html?d=lijiadong.cn`；服务器抓包显示请求到达容器后收到对端方向 `RST` |
| 外部公网 HTTPS | 阻塞 | 本机强制解析 `lijiadong.cn:443 -> 81.70.251.90` 后 TCP 可连通，但 TLS ClientHello 后 `SSL_ERROR_SYSCALL`；服务器抓包显示 ClientHello 到达容器后收到对端方向 `RST` |
| 腾讯云控制台可操作性 | 阻塞 | 本机无腾讯云 CLI 凭证；Chrome 未运行且未安装 Codex Chrome Extension，无法直接代操作控制台 |

### 阻塞判断

应用侧已完成 HTTPS 强制入口配置，服务器本机 HTTPS、健康检查和登录接口均验证通过。当前剩余问题发生在公网域名接入/云侧策略层：HTTP Host 被 DNSPod webblock 伪响应，HTTPS 在 TLS ClientHello 后被对端方向 RST 断开。域名注册状态正常、实名认证通过、DNS 服务器正常不能证明网站访问已备案接入放行；需要通过腾讯云控制台检查并处理 `lijiadong.cn` 的 ICP 备案、接入备案或域名管控状态。若已备案，需要向腾讯云提交工单附上上述 `webblock.html?d=lijiadong.cn` 与服务器抓包 RST 证据。
