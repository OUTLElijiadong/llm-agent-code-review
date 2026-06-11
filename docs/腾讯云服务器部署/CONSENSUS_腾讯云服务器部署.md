# CONSENSUS_腾讯云服务器部署

## 需求描述

在腾讯云服务器 `81.70.251.90` 上部署当前项目，使用户可以通过浏览器访问前端页面，并通过 nginx 同源访问后端 API。

## 验收标准

- `http://81.70.251.90` 返回前端页面 HTTP 200。
- `http://81.70.251.90/api/...` 能通过 nginx 转发到后端。
- `http://81.70.251.90/docs` 可访问 Swagger 文档。
- `mysql`、`backend`、`frontend` 三个容器处于运行状态。
- MySQL 健康检查通过，后端日志无启动失败错误。

## 技术方案

- 服务器安装 `docker-ce` 与 `docker-compose-plugin`。
- 将当前项目同步到 `/opt/code-review`。
- 使用 `/opt/code-review/deploy/.env` 注入生产环境变量。
- 在 `/opt/code-review/deploy` 执行 `docker compose up -d --build`。
- 使用 curl 与 `docker compose ps/logs` 做上线验证。

## 技术约束

- 不提交 `.env`。
- 不在文档中输出任何密钥明文。
- 不删除 Docker 数据卷。
- 保持现有 Docker Compose 架构，不引入额外服务。
