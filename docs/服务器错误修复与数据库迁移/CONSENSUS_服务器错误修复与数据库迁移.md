# 服务器错误修复与数据库迁移 - Consensus

## 需求描述

修复 `https://lijiadong.cn` 线上接口出现的服务器内部错误，并把本机当前 MySQL 数据库迁移到腾讯云服务器，确保前端页面和核心 API 能正常访问本机已有项目、文件、审查任务和问题数据。

## 验收标准

- HTTPS 首页 `https://lijiadong.cn/` 返回 200。
- HTTP `http://lijiadong.cn/` 自动跳转到 HTTPS。
- 管理员登录接口返回 200，并能获取有效访问令牌。
- 迁移后线上数据库表结构与本机当前数据库一致，至少包含 `user_api_config`、`repo_change_set`、`repo_change_set_item`、`alembic_version`。
- 迁移后核心业务表数据量与本机迁移源一致。
- 登录态核心 API 不再返回 500，覆盖项目、规则、审查任务、问题、报告、仪表盘、Agent、AI 配置等模块。
- 线上现有数据库已生成可回滚备份。

## 技术方案

- 使用 `mysqldump` 从本机 `cr_mysql` 导出当前业务库。
- 使用 `mysqldump` 在服务器备份现有业务库到 `/opt/code-review/backups/`。
- 暂停线上后端写入，导入本机 dump 到线上 MySQL。
- 重启后端服务，执行登录态 API 巡检。

## 约束

- 不在仓库保存数据库 dump。
- 不输出数据库密码、Root 密码、JWT、API Key 等敏感信息。
- 如导入失败，使用服务器备份恢复。
