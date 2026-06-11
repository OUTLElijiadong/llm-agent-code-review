# FINAL_域名HTTPS配置

## 交付结果

`lijiadong.cn` 已完成 HTTPS 配置，当前可通过 `https://lijiadong.cn/` 访问代码审查平台。

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
