# 报告排版与覆盖口径修复验收

## 候选与修复范围

- 候选版本：`4.1.9`；生产基线：`4.1.8`。
- 已发布的前序修复：生产 `4.1.7` 期间浏览器验收发现渗透委托列表仅凭 `report_task_id` 显示报告入口，审查员点开后被服务端拒绝；4.1.8 已按权限、报告关联和非空正文计算 `can_view_report`，无权响应不返回 `report_task_id`，列表与详情 UI 仅在 `can_view_report=true` 时显示入口。
- 本次生产复现：审查员打开代码审查报告 `#180`，封面统计显示 6 条问题，但正文没有逐条列出全部问题；没有有效 CVSS 向量的问题无法进入 Top 10。问题列表接口默认只返回未修复/待复查，无法覆盖报告中的所有状态。
- 本次修复：报告详情在既有 `report:view`、来源权限及 `issue:view` 门禁内，使用已有问题接口 `status=all` 分页读取，逐条展示标题、严重度、状态、位置、证据/复核状态和存在的描述、建议、修复方案、修复代码；Markdown 继续通过现有 DOMPurify 渲染器。没有新增后端接口或修改生产报告数据。

## 实现级验收

| 检查 | 结果 | 证据 |
|---|---|---|
| 根因复现与角色边界 | 通过 | 初始回归在服务端尚未返回 `can_view_report` 时因 `KeyError` 失败；空白正文回归复现为 `can_view_report=true`；移除唯一关联条件可复现重复委托仍显示入口。最终测试验证项目成员审查员无入口、有效报告发起人和管理员可读、缺少 `REPORT_VIEW` 或 `PENTEST_VIEW` 时拒绝、空白正文与重复关联不返回报告 ID。 |
| 后端定向回归 | 通过 | `backend/tests/unit/services/test_report_dashboard_source_facts.py` 与 `backend/tests/unit/test_pentest_service.py`：159 passed。 |
| 前端定向回归 | 通过 | `PentestList.regression.test.ts` 与 `PentestDetail.owasp.test.ts`：2 个测试文件、5 项测试通过。 |
| 静态检查 | 通过 | Ruff 检查本次修改的 Python 源码和测试；ESLint 检查渗透列表、详情、类型及列表回归测试；`git diff --check` 无错误。 |
| 前序修复构建 | 通过 | 4.1.8 前序修复的 `npm run build` 成功，Vite 转换 3,759 个模块。 |
| 报告问题明细回归 | 通过 | `ReportDetail.permissionMounted.test.ts` 验证无 CVSS、待人工核实、已修复条目均可查看完整说明/建议与状态；Markdown `**` 不裸露且 HTML 不执行；无问题权限时继续隐藏明细；全部分页请求使用 `status=all`。 |
| 大问题集浏览回归 | 通过 | 1,001 条记录全部读入并统计，页面仅渲染当前 20 条；翻到第 51 页可读取最后 1 条。 |
| 定向前端测试 | 通过 | `ReportDetail.test.ts` 与 `ReportDetail.permissionMounted.test.ts`：2 个测试文件、55 项测试通过。jsdom 输出已有的锚点导航未实现提示，不影响通过结果。 |
| 全量前端测试 | 通过 | Vitest：154 个文件、2,047 项测试通过。 |
| 静态检查与差异 | 通过 | `npm run lint` 和报告页面/测试定向 ESLint 通过；`git diff --check` 无错误。 |
| 4.1.9 最终候选构建 | 通过 | 语义版本已更新为 4.1.9 后，`npm run build` 成功，Vite 转换 3,759 个模块。 |
| 发布脚本回归 | 通过 | `deploy/tests/test_scripts.sh`：版本绑定 33 项通过；shell、运维及部署失败注入/回滚场景通过。 |
| 版本一致性 | 通过 | `VERSION`、后端配置、前端构建信息、Compose 与 Dockerfile 默认值均为 `4.1.9`。 |

## 生产验收

待 4.1.9 正式部署后补充 release SHA、部署事务、健康与 HTTPS 检查，以及已登录审查员账号对报告 #180 问题清单、无权报告拒绝、现存沙箱/渗透报告和页面滚动复验。管理员用量图引用前序真实管理员验收；本轮登录身份是审查员，未重复管理员操作。远程网站评估没有现存任务或授权目标，不能发起新扫描，故列为未测。当前尚未对 4.1.9 宣布生产部署完成。
