# 验收记录

## 本地

- 后端反编译单元测试：17 passed。
- 后端反编译、沙箱反编译、多 Agent、优化、报告发布与执行器专项：81 passed。
- 前端全量测试：35 个测试文件、236 passed。
- 前端生产构建：`vue-tsc && vite build` 通过。
- `git diff --check` 通过。
- 全仓 ESLint 仍有 6 个既有错误，位于 `AgentTeamSidePanel.vue`、`ResponseToolTimeline.vue`、`FluidProgress.vue`；本次改动文件未新增 lint 错误。

## 公网

- 待执行：部署后健康检查、认证白盒启动、卡片关闭重开/历史恢复。
- 待执行：记录部署版本、容器健康状态和错误日志检查结果。
