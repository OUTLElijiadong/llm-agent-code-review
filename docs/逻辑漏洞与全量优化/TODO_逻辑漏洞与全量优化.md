# TODO_逻辑漏洞与全量优化

## 待办事项

1. 在 Docker 或 Caddy 可用环境补跑 Caddyfile 语法验证：

```bash
docker run --rm -v "$PWD/frontend/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2 caddy validate --config /etc/caddy/Caddyfile
```

2. 若生产环境需要连接内网模型服务，先确认网络隔离和可信目标，再在后端环境变量中显式配置：

```bash
ALLOW_PRIVATE_AI_BASE_URL=true
```

3. 若生产 DNS 不使用 fake-IP/代理解析，可加强用户自定义 API 域名防护：

```bash
ENFORCE_AI_BASE_URL_DNS_CHECK=true
```

4. 腾讯云/DNSPod 域名拦截仍需按 `docs/域名HTTPS配置/TODO_域名HTTPS配置.md` 到控制台处理备案/接入备案/域名管控状态；本轮没有控制台凭证，无法自动完成。

5. 后续可继续把用户登录 token 从 localStorage 迁移到 HttpOnly Cookie + CSRF 防护，但这属于认证架构升级，需单独 6A 评估，避免破坏现有前端和 WebSocket/SSE 鉴权。

