# 验收：Responses 审批闭环

## 本地候选验证

- 修复前隔离复现：通用批准曾返回 200 并只把审批置为 `approved`，绑定 run 仍为 `waiting_approval`。
- 修复后定向 API 文件：18 passed，含双字段一致、payload 为空、payload 指向不存在 run、resource 指向不存在 run，并对 approve/reject 均验证 400 与状态不变；旧式无 run 审批保持兼容。
- 完整后端最终运行：5972 passed、5 skipped、6 warnings，83% coverage，343.82 秒，退出码 0；日志为 `docs/全系统架构与体验审计20261001/evidence/复测-20261005-Responses审批闭环后端全量最终.log`。
- 前端完整测试：137 文件、1706 测试通过；ESLint 通过；`vue-tsc` 与 Vite 生产构建通过。
- Ruff、compileall、`git diff --check` 通过；部署脚本绑定场景 33/33、故障恢复场景 20/20 通过。
- 本地测试使用 Python 3.11.15、pytest 8.4.2 的共享虚拟环境加载本工作树源码；发布容器将按仓库锁文件独立构建，二者不可混称同一运行环境。

## 生产验收

生产 #234 仅执行只读核验；不会用真实审批行进行批准/驳回回归。版本、SHA、健康、运维检查和审批状态须在部署后填入证据。生产 API/页面仅能证明本次实际观察到的范围，不扩大为全系统验收。
