# ALIGNMENT_腾讯云服务器部署

## 原始需求

将当前“基于大模型智能体的代码审查平台”部署到腾讯云服务器，服务器公网 IP 为 `81.70.251.90`，使用 `root` 账号登录。

## 项目上下文

- 技术栈：FastAPI + Vue3 + MySQL 8.0。
- 现有部署方式：`deploy/docker-compose.yml` 编排 `mysql`、`backend`、`frontend` 三个服务。
- 前端容器当前使用 Caddy 暴露 `80/443` 端口，并反向代理 `/api/`、`/api/ws/`、`/docs`、`/openapi.json` 到后端。
- 后端容器读取 `deploy/.env`，数据库连接在 Compose 内固定为 `mysql:3306`。
- 敏感配置：`deploy/.env` 已存在并被 `.gitignore` 忽略，不写入仓库。

## 边界确认

- 本次只完成单机 Docker Compose 部署，不扩展 Kubernetes、HTTPS 证书、域名解析或 CI/CD。
- 保留 MySQL Docker 卷作为持久化数据位置。
- 不在文档中记录服务器密码、数据库密码、JWT 密钥或大模型 API Key。

## 已确认条件

- 服务器系统：OpenCloudOS 9.4。
- 服务器磁盘：根分区约 40GB，可用空间满足首次构建。
- 端口：`80`、`8000`、`3307` 初始未占用。
- Docker 初始未安装，需先安装 Docker Engine 与 Compose 插件。

## 疑问与决策

| 问题 | 决策 |
|---|---|
| 是否需要域名/HTTPS | 已通过 `lijiadong.cn` 和 Caddy 自动 HTTPS 补充配置 |
| 是否需要公网开放后端 8000 | 保留 Compose 现有映射，主要访问入口为 Caddy `80/443` |
| 是否使用已有本地 `.env` | 使用 `deploy/.env` 作为生产环境变量来源 |
