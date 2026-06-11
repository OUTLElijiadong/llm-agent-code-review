# TODO_域名HTTPS配置

## 待办

- **必须先处理当前阻塞**：从腾讯云控制台确认实例状态正常，必要时重启实例或重启 sshd；当前本机 SSH 到 `81.70.251.90:22` 能建立 TCP 但收不到 SSH banner，导致配置无法继续同步。
- 检查腾讯云安全组 / 云防火墙 / 主机安全策略：确认 `80`、`443`、`22` 没有被策略拦截或进入防护封禁；`8000`、`3307` 不需要公网放行。
- 检查 `lijiadong.cn` 的 ICP 备案、域名接入策略、CDN/WAF 是否启用或指向错误源站；当前外部 HTTPS 多节点在 TLS/HTTP 阶段 reset/timeout，服务器本机 HTTPS 曾验证正常。
- SSH 恢复后执行同步：上传 `frontend/Caddyfile` 与 `deploy/docker-compose.yml` 到 `/opt/code-review`，执行 `cd /opt/code-review/deploy && docker compose --env-file .env up -d --no-build`。
- 同步后复测：`https://lijiadong.cn/`、`/docs`、`/healthz`、`/api/auth/login`、`/api/agents/events?replay=0`、`/api/ws/discuss/{session_id}` 均需通过 HTTPS/WSS 网关访问。
- 如需 `www.lijiadong.cn`，另增 `www A 81.70.251.90`，并同步扩展 Caddy 配置。
- 本机或个别网络若短时间仍解析到旧地址，等待 DNS 缓存过期后再刷新。
- 后续可在腾讯云控制台确认 TCP `443` 安全组规则长期保留。
