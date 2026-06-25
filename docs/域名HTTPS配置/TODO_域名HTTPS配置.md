# TODO_域名HTTPS配置

## 待办

- **必须先处理当前阻塞**：在腾讯云控制台检查 `lijiadong.cn` 的 ICP 备案/接入备案/域名管控状态。当前外部 HTTP Host 明确返回 `https://dnspod.qcloud.com/static/webblock.html?d=lijiadong.cn`，不是 Caddy 或后端返回。
- 若域名尚未备案：按腾讯云 ICP 备案流程完成 `lijiadong.cn` 备案，备案通过并解除拦截后再做公网 HTTPS 复测。
- 若域名已备案：向腾讯云提交工单，说明 `lijiadong.cn` 已 A 记录到 `81.70.251.90`，服务器侧 HTTPS 和登录接口均正常，但公网访问出现 DNSPod webblock/TLS reset，请求解除域名拦截或同步备案接入状态。
- 控制台建议检查路径：
  1. 腾讯云控制台 → ICP 备案：确认 `lijiadong.cn` 是否存在备案号，且备案状态为已通过。
  2. 腾讯云控制台 → ICP 备案 → 接入备案/新增服务：若域名曾在其他服务商备案，需把接入商变更或新增接入到腾讯云。
  3. DNSPod 域名控制台 → `lijiadong.cn`：确认 `@` 的 A 记录为 `81.70.251.90`，并检查是否存在“网站拦截/未备案拦截/风险拦截”提示。
  4. 轻量应用服务器控制台 → 防火墙/安全组：确认 `80`、`443` 开放；`8000`、`3307` 不需要开放。
- 检查腾讯云安全组 / 云防火墙 / 主机安全策略：确认 `80`、`443`、`22` 没有被策略拦截或进入防护封禁；本项目 `8000`、`3307` 已收敛为 `127.0.0.1` 绑定，不需要公网放行。
- 阻塞解除后复测：`https://lijiadong.cn/`、`/docs`、`/healthz`、`/api/auth/login`、`/api/agents/events?replay=0`、`/api/ws/discuss/{session_id}` 均需通过 HTTPS/WSS 网关访问。
- 如需 `www.lijiadong.cn`，另增 `www A 81.70.251.90`，并同步扩展 Caddy 配置。
- 本机当前 `dig lijiadong.cn` 返回 `198.18.0.22`，属于本地网络/代理 fake-IP 解析；测试真实源站时需关闭代理或使用 `--resolve lijiadong.cn:443:81.70.251.90`。
- 后续可在腾讯云控制台确认 TCP `443` 安全组规则长期保留。

## 当前不可由服务器侧继续修复的原因

- 服务器本机访问 `https://lijiadong.cn/`、`/login`、`/healthz`、`/docs` 均为 `200`。
- 服务器本机访问 `http://lijiadong.cn/login` 为 `301 https://lijiadong.cn/login`，符合 Caddy 配置。
- 公网强制解析 `lijiadong.cn:80 -> 81.70.251.90` 返回 DNSPod `webblock.html?d=lijiadong.cn`。
- 服务器抓包显示公网 HTTP `GET /login` 已到达 `cr_frontend` 容器，容器 ACK 后收到对端方向 `RST`，客户端同时拿到 DNSPod webblock 伪响应。
- 服务器抓包显示公网 HTTPS ClientHello 已到达 `cr_frontend` 容器，容器 ACK 后收到对端方向 `RST`，TLS 握手无法进入证书返回阶段。
- 这说明域名注册、实名认证、DNS 解析正常并不等于网站访问接入已放行；当前更像腾讯云/运营商链路基于 Host/SNI 的备案接入拦截。
- 本机未配置腾讯云 CLI 凭证；Chrome 未运行且未安装 Codex Chrome Extension，无法直接代操作已登录的腾讯云控制台。
