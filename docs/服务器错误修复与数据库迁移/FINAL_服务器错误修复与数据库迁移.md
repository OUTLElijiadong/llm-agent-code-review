# 服务器错误修复与数据库迁移 - Final

## 状态

已完成。

## 修复结论

线上“服务器内部错误”已定位为数据库结构与当前代码版本不一致：

- `review_rule` 缺少 `language` 字段，导致 `/api/rules` 500。
- 缺少 `user_api_config` 表，导致 `/api/api-config` 500。

已将本机当前 MySQL 数据库整体迁移到服务器，并修复迁移后扩展巡检发现的 PDF 导出字体问题。

## 迁移记录

- 服务器原库备份：`/opt/code-review/backups/code_review_before_local_migration_20260612_005828_valid.sql.gz`。
- 本机迁移源 dump：`/tmp/code_review_local_current_20260612_005828_valid.sql.gz`。
- 服务器迁移 dump：`/opt/code-review/backups/code_review_local_current_20260612_005828_valid.sql.gz`。
- 导入策略：停止后端写入，使用服务器 MySQL root 账号导入，再重启后端。

## 修复记录

- 修改 `backend/app/exporters/pdf_exporter.py`：
  - 优先注册系统/Noto 中文字体。
  - 无字体文件时使用 ReportLab 内置 `STSong-Light` CID 字体。
  - 不再使用 `TTFont("ChineseFont", "Helvetica")`。
  - 标题和表格统一使用已注册字体。
- 新增 `backend/tests/unit/utils/test_pdf_exporter.py`，覆盖缺少系统字体文件时的 PDF 导出路径。

## 最终验证

- 线上核心表：`user=2`、`project=8`、`code_file=500`、`review_task=24`、`review_issue=138`、`review_rule=116`、`ai_call_log=265`。
- `https://lijiadong.cn/` 返回 `HTTP/2 200`。
- `http://lijiadong.cn/` 返回 `301` 到 HTTPS。
- `https://lijiadong.cn/docs` 返回 `HTTP/2 200`。
- 登录态 API 巡检覆盖 44 个只读接口/导出接口，结果 `failed=0`。
- 写入/危险路由受控接线验证覆盖 42 个接口，结果 `failed=0`。
- 圆桌讨论预检返回 `200`，Agent SSE 返回 `HTTP 200` 且收到事件数据。
- Word/PDF 报告导出均返回 `200`。
- 最新后端日志无新的 500 或异常栈。
