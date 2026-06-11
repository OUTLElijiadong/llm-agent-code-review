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

初次配置时 `https://lijiadong.cn` 已完成服务器侧与公网验收；2026-06-12 复核发现外部公网链路出现 reset/timeout，最新状态以追加复核记录为准。

## 2026-06-12 HTTPS 强制入口复核

| 检查项 | 状态 | 结果 |
|---|---|---|
| 服务器侧 HTTPS 首页 | 已完成 | 服务器本机 `curl -I https://lijiadong.cn/` 返回 `HTTP/2 200` |
| 服务器侧 HTTPS 登录 | 已完成 | 服务器本机 `POST https://lijiadong.cn/api/auth/login` 返回 `200` |
| 证书兼容性 | 已完成 | 已重新签发 Let’s Encrypt RSA 证书，公钥算法 `rsaEncryption` |
| Caddy HTTPS 策略 | 已完成 | 本地配置已限制协议为 `h1 h2`，HTTP 全量跳 HTTPS，HTTPS 开启 HSTS |
| API 端口策略 | 已完成 | 本地 Compose 已将 `8000`、`3307` 改为 `127.0.0.1` 绑定，公网 API 统一走 `443` |
| 本地配置校验 | 已完成 | `bash -n deploy/deploy.sh`、`cd deploy && docker compose config --quiet` 均通过 |
| 服务器同步 | 阻塞 | SSH 到 `81.70.251.90:22` TCP 可建立，但卡在 banner 阶段，`rsync` 无法上传最新配置 |
| 外部公网 HTTPS | 阻塞 | 本机与 Check-Host 多节点对 `https://lijiadong.cn/` 出现 `Connection reset by peer` 或超时 |

### 阻塞判断

应用侧已完成 HTTPS 强制入口配置；服务器本机 HTTPS 与登录接口曾验证通过。当前剩余问题发生在公网链路/云侧策略层：TCP 端口可建立连接，但 HTTPS/SSH 在应用层握手前后超时或被 reset。需要先恢复 SSH 或通过腾讯云控制台检查实例网络、安全组、云防火墙/主机安全、ICP备案及域名接入策略，然后再执行服务器同步与线上复测。
