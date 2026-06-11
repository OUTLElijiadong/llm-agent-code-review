# ACCEPTANCE_腾讯云服务器部署

## 验收记录

| 检查项 | 状态 | 结果 |
|---|---|---|
| 项目部署配置检查 | 已完成 | `deploy/docker-compose.yml` 可解析，`deploy/.env` 已存在 |
| 服务器运行环境安装 | 已完成 | 已安装 Docker 28.0.1 与 Docker Compose 2.32.1 |
| 项目同步 | 已完成 | 当前工作区已同步到 `/opt/code-review`，排除 `.git`、依赖目录、构建产物和根目录 `.env` |
| Compose 构建与启动 | 已完成 | `cr_mysql`、`cr_backend`、`cr_frontend` 已启动，MySQL 为 `healthy` |
| 前端访问验证 | 已完成 | `http://81.70.251.90/` 返回 HTTP 200 |
| API 文档验证 | 已完成 | `http://81.70.251.90/docs`、`/openapi.json` 返回 HTTP 200 |
| 后端健康验证 | 已完成 | 服务器本地 `http://127.0.0.1:8000/healthz` 返回 `{"status":"ok"}` |
| API 代理验证 | 已完成 | 公网 `/api/agents/runtime` 到达后端并返回认证缺失的业务错误 |
| 登录验证 | 已完成 | 公网 `/api/auth/login` 使用内置管理员账号返回 HTTP 200 且包含 token |
| 数据库初始化验证 | 已完成 | MySQL 已创建 15 张业务表 |

## 风险记录

- 腾讯云安全组若未放行 `80` 端口，即使容器正常，本地浏览器也可能无法访问。
- 已补充域名与 HTTPS 配置，正式访问地址为 `https://lijiadong.cn/`。
- 服务器内存约 2GB，前端 Vite 构建需设置 `NODE_OPTIONS=--max-old-space-size=1536` 并补充 swap。
- 公网 `8000` 端口当前超时，后端直连调试不可用；正常访问通过 Caddy `80/443` 代理完成。

## 验收结论

本次腾讯云单机 Docker Compose 部署已完成，公网 HTTP 访问、API 代理、数据库初始化和管理员登录均验证通过。
