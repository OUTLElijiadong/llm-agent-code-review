# 服务器错误修复与数据库迁移 - Acceptance

## 执行记录

- [x] 读取 `说明文档.md` 并确认项目架构。
- [x] 复现线上 500：`/api/rules`、`/api/api-config`。
- [x] 定位根因：线上数据库结构落后于当前代码。
- [x] 备份线上现有数据库：`/opt/code-review/backups/code_review_before_local_migration_20260612_005828_valid.sql.gz`。
- [x] 导出本机当前数据库：`/tmp/code_review_local_current_20260612_005828_valid.sql.gz`。
- [x] 上传迁移 dump 到服务器：`/opt/code-review/backups/code_review_local_current_20260612_005828_valid.sql.gz`。
- [x] 暂停线上后端并导入服务器数据库。
- [x] 验证线上数据库核心表：`user=2`、`project=8`、`code_file=500`、`review_task=24`、`review_issue=138`、`review_rule=116`、`ai_call_log=265`。
- [x] 验证 `/api/rules` 与 `/api/api-config` 均从 500 恢复为 200。
- [x] 扩展只读 API 巡检发现 `/api/reports/35/export/pdf` 500。
- [x] 修复 PDF 导出字体注册逻辑，避免把 `Helvetica` 当作 TTF 文件注册；缺少系统字体时回退到 ReportLab 内置 `STSong-Light` CID 字体。
- [x] 新增 `backend/tests/unit/utils/test_pdf_exporter.py` 回归测试。
- [x] 部署后端修复并重建服务器后端容器。
- [x] 验证线上 API 与导出功能。
- [x] 更新总结文档。

## 验证结果

- 本机 PDF 最小生成验证通过：输出以 `%PDF-` 开头，大小大于 1000 字节。
- 单元测试：`python3 -m pytest -o addopts='' tests/unit/utils/test_pdf_exporter.py -q`，结果 `1 passed`。
- 编译检查：`python3 -m compileall app/exporters/pdf_exporter.py tests/unit/utils/test_pdf_exporter.py` 通过。
- HTTPS 首页：`https://lijiadong.cn/` 返回 `HTTP/2 200`。
- HTTP 跳转：`http://lijiadong.cn/` 返回 `301` 到 HTTPS。
- Swagger：`https://lijiadong.cn/docs` 返回 `HTTP/2 200`。
- 登录态 API 巡检：44 个只读接口/导出接口全部 `200`，失败数 `0`。
- 写入/危险路由受控接线验证：42 个接口使用无效请求体或不存在 ID 验证到达后端，均返回受控 `2xx/4xx`，失败数 `0`，未触发真实审查或外部模型调用。
- 圆桌讨论预检：`GET /api/discuss/start?project_id=...&file_id=...&review_type=discuss` 返回 `200`。
- Agent SSE：`GET /api/agents/events` 返回 `HTTP 200`，4 秒采样收到事件数据。
- Word 导出：`/api/reports/35/export/word` 返回 `200`。
- PDF 导出：`/api/reports/35/export/pdf` 返回 `200 application/pdf`。
- 最新后端日志未发现新的 `500`、`Traceback`、`ERROR`、`OperationalError`、`ProgrammingError`、`TTFError`。
