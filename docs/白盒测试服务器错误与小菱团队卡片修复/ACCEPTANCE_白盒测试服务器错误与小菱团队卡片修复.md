# 验收记录

## 本地

- 后端反编译、沙箱反编译、多 Agent、优化、报告发布与执行器专项：82 passed。
- 前端全量测试：35 个测试文件、238 passed。
- 前端生产构建：`vue-tsc && vite build` 通过。
- 前端全仓 ESLint：0 error、0 warning。
- 后端 Ruff：通过；`deploy/sandbox/runner.sh` shell 语法检查通过。
- `git diff --check` 通过。

## 公网

- 部署 SHA：`4b0237050a43269a53c608068b2b0a1bca661afb`；后端、前端镜像均使用该 SHA。
- 发布时间：`2026-08-19T02:12:40Z`；数据库备份：`../backups/code_review_20260819T020912Z_4b0237050a43.sql.gz`；隔离恢复校验 84 张表，Alembic `036`。
- `cr_backend`、`cr_frontend`、`cr_mysql`、`cr_redis`、`cr_clamav` 均为 `healthy`；`https://www.lijiadong.cn/` 根入口返回 `200`。
- PHP worker 镜像已按固定基础镜像摘要重建并固化：`sha256:70e524b4b8722efb53f3033602372dee0ae4727a37c9f69eae4002abae860c1e`；runner SHA-256 为 `2a3092b8ee18eded75c58ed72dd1a2f6baa9665d24aed1f33541af356047f644`，executor 重启后为 active。
- 公网白盒任务 `sbx_caf6d82214ee4076960c040a` 创建成功，生产日志无该请求 5xx。`bWAPP-master.tar.gz` 被正确解析并归一化为 worker 可执行 ZIP，部署核验识别入口 `bWAPP-master/app/index.php`。
- 白盒 worker 实际执行 3 个动态测试，3/3 通过；PHP Deprecated 输出保留为 warning，不再误判为语法失败。任务记录 `PRISM_WHITEBOX_DONE {"executed":true,"passed":true}`，worker `exit_code=0`、`outcome=succeeded`。
- 最终任务状态为“已通过”，产生 6 个报告工件（含 JUnit、SARIF、HTML 和 16.2 KiB Markdown 审查报告）。
- 小菱公网验收：历史对话中团队卡片和结论可见；关闭悬浮窗后重开仍可见；新建对话后重新打开历史对话仍可见。
