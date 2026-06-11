# 服务器错误修复与数据库迁移 - Align

## 原始需求

用户反馈线上服务器显示“服务器内部错误”，要求修复并确保所有功能和 API 接上，同时把当前本机数据库迁移到腾讯云服务器。

## 项目上下文

- 后端：FastAPI + SQLAlchemy + MySQL。
- 前端：Vue3 SPA，通过 `/api` 访问后端。
- 线上部署：腾讯云 `81.70.251.90`，项目目录 `/opt/code-review`，Compose 目录 `/opt/code-review/deploy`。
- HTTPS：`https://lijiadong.cn` 由 Caddy 提供 TLS、静态文件服务和 `/api/*` 反向代理。
- 本机当前数据库容器：`cr_mysql`，端口映射 `3307:3306`。
- 线上数据库容器：`cr_mysql`，Compose 卷 `mysql_data`。

## 已确认故障

- `GET /api/rules` 返回 500，后端异常为 `Unknown column 'review_rule.language' in 'field list'`。
- `GET /api/api-config` 返回 500，后端异常为 `Table 'code_review.user_api_config' doesn't exist`。
- 本机数据库包含 `project/code_file/review_task/review_issue/ai_call_log/review_rule/user_api_config` 等完整数据和新表结构。
- 线上数据库仍接近初始化状态，缺少新字段和新表，属于数据库结构与当前代码版本不一致。

## 边界确认

- 本次只处理线上 500、线上数据库结构/数据迁移、线上 API 连通性验证。
- 不改动业务功能逻辑，除非迁移后验证发现代码层缺陷。
- 迁移前必须备份线上现有数据库。
- 数据库 dump 文件不得提交到 Git。

## 关键决策

- 以本机 `cr_mysql` 当前库作为权威数据源。
- 线上现有库先备份，再整体导入本机当前库，保持表结构和业务数据一致。
- 迁移后用登录态 API 巡检验证核心模块。
