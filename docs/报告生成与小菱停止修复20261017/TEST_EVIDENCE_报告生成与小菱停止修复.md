# 报告生成与小菱停止修复测试证据

日期：2026-10-17
工作树：`/Users/li/.codex/worktrees/prism-v4-0-109-release/基于大模型智能体的代码审查平台`
分支：`codex/prism-v4-1-2-release`
版本：`4.1.2`
本轮增量开始时 HEAD：`ca009442d8d103403e8bb3919e600c234fdf74bb`
先前候选 SHA：`dff10078f7bf5ae9b5687c3821888cb217c07456`；其后文档提交 `ca009442d8d103403e8bb3919e600c234fdf74bb` 的业务回归/发布检查 39017057141 和 CodeQL 39017057167 均成功。该 CI 尚未覆盖本轮新增代码。
当前增量代码已完成本地回归，正在固定最终 SHA 并触发新的 GitHub 完整 CI；在新 SHA 工作流成功前，不把旧 SHA 的 CI 当作本轮代码证据。

## 本轮增量回归

| 检查 | 命令 | 结果 |
|---|---|---|
| 后端报告、渗透、停止与权限回归 | `backend/.venv/bin/python -m pytest backend/tests/unit/test_pentest_service.py backend/tests/unit/services/test_security_report_service.py backend/tests/unit/services/test_remote_pentest_service.py backend/tests/unit/services/test_agent_responses_admin_tools.py backend/tests/unit/services/test_sandbox_report_publish.py backend/tests/unit/services/test_sandbox_multi_agent_review.py backend/tests/unit/test_discussion_orchestrator_coverage.py backend/tests/unit/agents/test_ws_discussion_coverage.py backend/tests/unit/services/test_viewer_execution_boundaries.py -q -o addopts=''`（工作目录仓库根目录） | 9 个测试文件、398 passed，200 warnings，37.67 秒，退出码 0；覆盖未知/部分、失败与取消、预启动取消报告、圆桌工具启动、沙箱报告完整性和权限边界 |
| 前端安全中心、管理员、登录与圆桌交互 | `npm test -- --reporter=dot src/views/security/SecurityCenter.test.ts src/views/admin/AdminSecurityCenter.test.ts src/views/admin/AdminOverview.test.ts src/views/auth/Login.test.ts src/components/agent/AgentDiscussionPanel.test.ts`（工作目录 `frontend/`） | 5 个测试文件、131 tests passed，退出码 0 |
| 前端静态检查 | `npm run lint`（工作目录 `frontend/`） | 退出码 0 |
| 前端类型检查及生产构建 | `npm run build`（工作目录 `frontend/`） | `vue-tsc` 通过；Vite 转换 3760 个模块并构建成功，退出码 0 |
| 修改过的 Python 文件 | `backend/.venv/bin/ruff check backend/app/services/pentest_service.py backend/app/services/sandbox_service.py backend/tests/unit/test_pentest_service.py backend/tests/unit/services/test_sandbox_report_publish.py backend/tests/unit/services/test_sandbox_multi_agent_review.py` | All checks passed |
| 差异空白检查 | `git diff --check` | 退出码 0 |
| 部署脚本回归 | `bash deploy/tests/test_scripts.sh` | Release binding 33 passed、部署故障注入矩阵全部通过、部署 shell 与运维测试通过，退出码 0 |
| 先前候选 SHA 全量 CI：后端 | GitHub Actions 工作流 39017057141 | 7,327 passed，5 warnings，84% 覆盖率；Ruff、字节码编译、依赖审计通过；不覆盖本轮增量代码 |
| 先前候选 SHA 全量 CI：前端、发布门禁 | GitHub Actions 工作流 39017057141 | 前端测试、ESLint、`vue-tsc`、Vite 构建、部署/执行器故障注入通过；不覆盖本轮增量代码 |
| 先前候选 SHA CodeQL | GitHub Actions 工作流 39017057167 | Python 与 JavaScript/TypeScript 扫描成功；不覆盖本轮增量代码 |

上述本地定向回归在本轮新增代码上重跑通过。新的固定 SHA 全量 CI 与 CodeQL 状态待推送后记录。

新增的沙箱回归先复现再修复：`test_repeated_stop_preserves_existing_full_sandbox_report` 在修复前稳定失败，证明重复停止会把完整 AI 报告降级成简化报告；当前实现只在已终态重复停止入口检查已有报告，报告存在时不重写。`test_new_terminal_failure_evidence_refreshes_existing_sandbox_report` 验证浏览器黑盒追加失败证据后仍会刷新任务状态和报告证据，并逐条检查多个旧附件与报告中心正文的 UTF-8 字节、大小及 SHA-256 一致；成功终态也校验附件与报告正文完全一致。`test_terminal_incomplete_ai_state_replaces_stale_complete_report` 覆盖数据库最新状态为 AI 未完成、旧报告却标记完成时的正文、元数据、任务摘要和附件同步。`test_unpersisted_complete_ai_report_is_replaced_by_persisted_deterministic_report` 覆盖 AI 报告首次入库失败后改用确定性报告并再次持久化，避免把用户打不开的 AI 报告标为完成。终态报告任务写入受环境行锁保护，任务、报告和现有附件使用锁定当前读，并在同一事务提交；测试库是 SQLite，仍需 MySQL 并发环境验证。

渗透覆盖回归新增 `retrying` 线路状态与 `reconciling` 阶段状态，确认未知状态会被列为未扫描并阻止报告显示完整。上述两类测试均包含在本轮 350 项后端定向回归中。

## 本轮之前的候选基线

以下数据来自本轮增量修改之前的候选基线，不代表当前增量树或最终发布 SHA 的全量测试：

- 后端：7442 passed、8 skipped，344.41 秒；1452 条弃用/收集类警告。
- 前端：151 个测试文件、2002 项通过；ESLint、`vue-tsc` 与 Vite 构建通过。
- 部署脚本回归此前有通过记录；本轮 `bash deploy/tests/test_scripts.sh` 复测也通过，包含 33 项 release binding 与部署故障注入矩阵。

代码候选 SHA 已固定并通过上述完整 CI。生产部署与真实账号验收尚未完成：线上 SSH 公钥认证拒绝，不能据此宣称正式上线。
