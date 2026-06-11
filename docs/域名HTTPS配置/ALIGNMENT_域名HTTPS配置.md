# ALIGNMENT_域名HTTPS配置

## 原始需求

为已部署在腾讯云服务器 `81.70.251.90` 的代码审查平台配置域名 `lijiadong.cn` 和 HTTPS。

## 当前状态

- 应用已部署在 `/opt/code-review`，前端入口当前通过 Docker Compose 暴露 `80`。
- `http://81.70.251.90/` 已可公网访问。
- `lijiadong.cn` 已添加到腾讯云控制台，DNS 状态显示正常。
- 初始公网解析曾显示 `198.18.0.43/198.18.0.44`；服务器端最终公共 DNS 检查已解析到 `81.70.251.90`。

## 边界确认

- 本次配置应用网关和 HTTPS 自动签发能力。
- DNS 根域记录最终解析到 `A -> 81.70.251.90`。
- 不在仓库或文档中记录任何腾讯云账号、API Key 或服务器密码。

## 关键决策

- 使用 Caddy 替代前端容器内 nginx，负责静态资源、API 反代和 Let’s Encrypt 自动 HTTPS。
- 持久化 Caddy 证书数据到 Docker volume，避免容器重建后重复签发。
- 先配置根域 `lijiadong.cn`；`www.lijiadong.cn` 可在后续补充对应 DNS 和 Caddy 站点。
