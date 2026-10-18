# 稳定部署与生产验收结项

## 正式发布

Prism 4.1.6 已于 2026-10-18 正式全量部署，生产提交 SHA 为 `e9e02858342bc4cb33119b070c09cfeb502493e6`。候选通过本地 Git bundle 导入生产仓库并由正式 `deploy.sh all --revision` 发布；GitHub 上传失败按用户要求跳过。没有改版本号，也没有重复部署。

备份与隔离恢复验证通过（恢复验证 109 张表）；Alembic 当前与唯一 head 均为 `064_remote_pentest_chain`。Backend、Frontend 使用同一 SHA 的镜像，外部 `/healthz` 与 `/readyz` 返回 200、HTTP 根路径 308 跳转 HTTPS、HTTPS 首页返回 200。部署后 `ops-check.sh` 为 `ok`，备份、容器、迁移、发布账本和 HTTPS 均通过。

## 自动化质量

- 后端全量：7,397 passed、6 skipped、0 failed；跳过项需要本机 Redis。
- 前端全量：154 个文件、2,042 passed；lint 与生产构建通过，3,759 个 Vite 模块。
- 变更 Python 文件 Ruff、`git diff --check` 及发布脚本故障恢复回归通过。
- 报告/权限定向回归 243 passed；小菱停止定向回归 122 passed，WebSocket 子集 17 passed。

## 尚未完成的真实业务验收

Safari 当前停在登录页，虽已确认页面构建版本为 4.1.6，但普通用户、审查员和管理员的登录态检查尚未执行。因此，真实报告卡片/详情/导出权限、管理员每日 Token 图与原始数据对账、各报告类型真实内容和排版仍需在对应账号中核验。CNVD/CNNVD 官方授权数据未接入，官方逐条匹配未完成；供应商侧已发请求是否停止计费没有账单证据。

这些未完成项不影响本次部署事务和自动化测试结论，但不能据此宣称所有真实账号及报告样本均已验收通过。详细状态与执行条件见 [ACCEPTANCE](./ACCEPTANCE_稳定部署与生产验收.md) 和 [TODO](./TODO_稳定部署与生产验收.md)。
