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

`https://lijiadong.cn` 已配置完成并通过公网验证。
