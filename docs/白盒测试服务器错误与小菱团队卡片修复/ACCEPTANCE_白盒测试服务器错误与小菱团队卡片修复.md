# 验收记录

## 本地

- 后端反编译、沙箱反编译、多 Agent、优化、报告发布与执行器专项：82 passed。
- 前端全量测试：35 个测试文件、238 passed。
- 前端生产构建：`vue-tsc && vite build` 通过。
- `git diff --check` 通过。
- 全仓 ESLint 仍有 6 个既有错误，位于 `AgentTeamSidePanel.vue`、`ResponseToolTimeline.vue`、`FluidProgress.vue`；本次改动文件未新增 lint 错误。

## 公网

- 部署 SHA：`be22b61a2097b350d8964fa117beccc449cb61f5`；后端、前端镜像均使用该 SHA。
- 发布时间：`2026-08-19T01:47:26Z`；数据库备份：`../backups/code_review_20260819T014503Z_be22b61a2097b350d8964fa117beccc449cb61f5.sql.gz`；隔离恢复校验 84 张表，Alembic `036`。
- `cr_backend`、`cr_frontend`、`cr_mysql`、`cr_redis`、`cr_clamav` 均为 `healthy`；`https://www.lijiadong.cn/` 根入口返回 `200`。
- 公网白盒任务 `sbx_eab9935fc88d47cd800a51e6` 创建接口返回 `200`，生产日志无该请求 5xx。`bWAPP-master.tar.gz` 被正确解析并归一化为 worker 可执行 ZIP，部署核验识别入口 `bWAPP-master/app/index.php`。
- 白盒 worker 已实际执行生成测试和事实提取，产生 6 个报告工件（含 JUnit、SARIF、HTML 和 Markdown 审查报告）。任务记录 `PRISM_WHITEBOX_DONE {"executed":true,"passed":false}`；失败原因是样本 PHP lint 问题，不是平台服务器错误。
- 小菱公网验收：历史对话中团队卡片和结论可见；关闭悬浮窗后重开仍可见；新建对话后重新打开历史对话仍可见。
