# FINAL_腾讯云服务器部署

## 交付结果

腾讯云服务器 `81.70.251.90` 已完成单机 Docker Compose 部署。

## 已完成事项

- 安装并启用 Docker 28.0.1 与 Docker Compose 2.32.1。
- 当前项目已同步到服务器 `/opt/code-review`。
- `deploy/.env` 已部署到服务器并设置为 `600` 权限。
- 后端 Dockerfile 改为腾讯云 Debian/PyPI 镜像源，避免默认源构建卡顿。
- 前端 Dockerfile 使用 npmmirror，并设置 `NODE_OPTIONS=--max-old-space-size=1536`。
- 服务器新增 2GB 持久化 swap：`/swapfile-code-review`。
- Compose 已启动 `cr_mysql`、`cr_backend`、`cr_frontend`。

## 验证结果

| 验证项 | 结果 |
|---|---|
| 前端页面 | `http://81.70.251.90/` HTTP 200 |
| Swagger | `http://81.70.251.90/docs` HTTP 200 |
| OpenAPI | `http://81.70.251.90/openapi.json` HTTP 200 |
| API 代理 | `/api/agents/runtime` 可达后端 |
| 后端健康 | 服务器本地 `/healthz` 返回 `{"status":"ok"}` |
| MySQL | 容器健康，15 张业务表已创建 |
| 登录 | 内置管理员登录 HTTP 200 且返回 token |

## 访问地址

- 前端：`http://81.70.251.90/`
- Swagger：`http://81.70.251.90/docs`
- OpenAPI：`http://81.70.251.90/openapi.json`
- HTTPS 正式域名：`https://lijiadong.cn/`

## 运维命令

```bash
cd /opt/code-review/deploy
docker compose --env-file .env ps
docker compose --env-file .env logs -f backend
docker compose --env-file .env up -d --build
```

## 注意事项

- 公网 `8000` 端口当前不可直连；应用正常访问不依赖该端口，API 通过 Caddy 代理。
- 域名和 HTTPS 已在 `docs/域名HTTPS配置/` 中补充记录。
- 密钥、数据库密码和大模型 API Key 仅存在于 `.env`，不写入文档和仓库。
