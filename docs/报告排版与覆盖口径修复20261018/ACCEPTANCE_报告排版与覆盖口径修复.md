# 报告排版与覆盖口径修复验收

## 候选与修复范围

- 候选版本：`4.1.8`。
- 基线版本：生产 `4.1.7`；浏览器验收发现渗透委托列表仅凭 `report_task_id` 显示报告入口，审查员点开后被服务端拒绝。
- 修复：服务端按报告查看权限、渗透报告来源权限、项目可见范围、发起人/管理员关系、可报告终态、唯一报告任务关联、报告快照关联及非空正文计算 `can_view_report`；无权或报告快照无有效正文时响应不返回 `report_task_id`。列表和详情 UI 仅在 `can_view_report=true` 时呈现入口。

## 实现级验收

| 检查 | 结果 | 证据 |
|---|---|---|
| 根因复现与角色边界 | 通过 | 初始回归在服务端尚未返回 `can_view_report` 时因 `KeyError` 失败；空白正文回归复现为 `can_view_report=true`；移除唯一关联条件可复现重复委托仍显示入口。最终测试验证项目成员审查员无入口、有效报告发起人和管理员可读、缺少 `REPORT_VIEW` 或 `PENTEST_VIEW` 时拒绝、空白正文与重复关联不返回报告 ID。 |
| 后端定向回归 | 通过 | `backend/tests/unit/services/test_report_dashboard_source_facts.py` 与 `backend/tests/unit/test_pentest_service.py`：159 passed。 |
| 前端定向回归 | 通过 | `PentestList.regression.test.ts` 与 `PentestDetail.owasp.test.ts`：2 个测试文件、5 项测试通过。 |
| 静态检查 | 通过 | Ruff 检查本次修改的 Python 源码和测试；ESLint 检查渗透列表、详情、类型及列表回归测试；`git diff --check` 无错误。 |
| 前端生产构建 | 通过 | `npm run build` 成功，Vite 转换 3,759 个模块。 |
| 版本一致性 | 通过 | `VERSION`、后端配置、前端构建信息、Compose 与 Dockerfile 默认值均为 `4.1.8`。 |

## 生产验收

待 4.1.8 正式部署后补充 release SHA、部署事务、健康与 HTTPS 检查，以及已登录审查员账号对渗透委托列表、详情、报告列表和有权报告的浏览器复验。管理员用量图引用前序真实管理员验收；本轮未登录管理员账号。远程网站评估没有现存任务或授权目标，不能发起新扫描，故列为未测。
