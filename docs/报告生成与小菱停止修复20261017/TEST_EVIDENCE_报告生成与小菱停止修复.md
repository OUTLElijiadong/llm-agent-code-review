# 报告生成与小菱停止修复测试证据

更新日期：2026-10-18
当前候选版本：`4.1.6`
当前候选代码提交：`ea13db0359e9db285d5b41da3ef3b1a3d924e731`
当前工作树：`/Users/li/.codex/worktrees/prism-v4-1-5-release/基于大模型智能体的代码审查平台`
当前分支：`codex/prism-v4-1-5-release`

下方 4.1.4 及更早内容是历史测试快照，不代表当前候选的验证结果。当前候选的权威结果以文末“v4.1.6 候选最终回归”为准。`3203ba31d029235aaa05f03cdf80a3d5422f03b7` 的 CI/CodeQL 不覆盖当前候选，因此不作为 4.1.6 的 CI 证据。

## 4.1.4 本地最终增量

| 检查 | 命令/方式 | 结果 |
|---|---|---|
| 后端全量回归 | `backend/.venv/bin/pytest -q backend/tests --no-cov` | 7,332 passed、6 skipped，1467 warnings，488.99 秒，退出码 0 |
| 维护脚本与摘要修复定向测试 | `backend/.venv/bin/pytest -q --no-cov backend/tests/unit/scripts/test_reconcile_confirmed_cleanup_report.py backend/tests/unit/services/test_sandbox_optimizations.py::test_heartbeat_recovery_creates_sandbox_stuck_alert backend/tests/unit/services/test_sandbox_optimizations.py::test_heartbeat_recovery_keeps_environment_stopping_when_worker_cleanup_fails` | 4 passed；新增修复先复现摘要冲突，再确认清理完成的终态摘要已纠正 |
| 前端完整测试 | `npm run test`（`frontend/`） | 151 个测试文件、2,010 项通过；jsdom 有预期的导航未实现提示，不影响退出码 0 |
| 前端静态检查与构建 | `npm run lint`、`npm run build` | ESLint 退出码 0；Vite 转换 3,760 个模块并构建成功 |
| 部署脚本故障注入回归 | `bash deploy/tests/test_scripts.sh` | release binding 33 项，发布/回滚故障注入矩阵通过，退出码 0 |
| Python 静态检查 | `backend/.venv/bin/ruff check`（改动服务、脚本与测试） | All checks passed |
| 补丁检查 | `git diff --check` | 退出码 0 |

历史记录：以上结果属于 4.1.4 候选阶段；后续最终候选及其生产状态见文末 4.1.6 回归记录。

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
| 来源权限负向回归 | `backend/.venv/bin/python -m pytest backend/tests/test_dashboard_private_report_scope.py backend/tests/test_report_api.py backend/tests/test_report_object_authorization.py backend/tests/test_report_project_scope_snapshot.py backend/tests/unit/services/test_report_service.py backend/tests/unit/services/test_report_dashboard_source_facts.py -q -o addopts=''` | 6 个测试文件、304 passed，11 warnings，12.21 秒，退出码 0；覆盖通用报告权限存在、来源权限缺失时报告列表、项目近期任务、摘要指标与权限判定均不可见 |
| 当前改动 Python 静态检查 | `backend/.venv/bin/ruff check backend/app/services/report_service.py backend/app/services/review_service.py backend/tests/test_dashboard_private_report_scope.py` | All checks passed，退出码 0；`git diff --check` 通过 |
| 候选 SHA `3203ba31` 全量后端 CI | GitHub Actions 工作流 [39018904067](https://github.com/OUTLElijiadong/llm-agent-code-review/actions/runs/39018904067) | 7,331 passed，5 warnings，84% 覆盖率；Ruff、字节码编译、依赖审计通过；不覆盖随后新增的来源权限修复 |
| 候选 SHA `3203ba31` 全量前端、发布门禁 | GitHub Actions 工作流 [39018904067](https://github.com/OUTLElijiadong/llm-agent-code-review/actions/runs/39018904067) | 前端完整测试、ESLint、`vue-tsc`、Vite 构建、部署/执行器故障注入均通过；不覆盖随后新增的后端权限修复 |
| 候选 SHA `3203ba31` CodeQL | GitHub Actions 工作流 [39018904018](https://github.com/OUTLElijiadong/llm-agent-code-review/actions/runs/39018904018) | Python 与 JavaScript/TypeScript 扫描成功；不覆盖随后新增的来源权限修复 |

历史记录：上述本地定向回归在当时新增代码上重跑通过。候选 `3203ba31` 的全量 CI 与 CodeQL 已成功，但不覆盖后续修改；它们不能替代当前候选的验证。

新增的沙箱回归先复现再修复：`test_repeated_stop_preserves_existing_full_sandbox_report` 在修复前稳定失败，证明重复停止会把完整 AI 报告降级成简化报告；当前实现只在已终态重复停止入口检查已有报告，报告存在时不重写。`test_new_terminal_failure_evidence_refreshes_existing_sandbox_report` 验证浏览器黑盒追加失败证据后仍会刷新任务状态和报告证据，并逐条检查多个旧附件与报告中心正文的 UTF-8 字节、大小及 SHA-256 一致；成功终态也校验附件与报告正文完全一致。`test_terminal_incomplete_ai_state_replaces_stale_complete_report` 覆盖数据库最新状态为 AI 未完成、旧报告却标记完成时的正文、元数据、任务摘要和附件同步。`test_unpersisted_complete_ai_report_is_replaced_by_persisted_deterministic_report` 覆盖 AI 报告首次入库失败后改用确定性报告并再次持久化，避免把用户打不开的 AI 报告标为完成。终态报告任务写入受环境行锁保护，任务、报告和现有附件使用锁定当前读，并在同一事务提交；测试库是 SQLite，仍需 MySQL 并发环境验证。

渗透覆盖回归新增 `retrying` 线路状态与 `reconciling` 阶段状态，确认未知状态会被列为未扫描并阻止报告显示完整。上述两类测试均包含在本轮 398 项后端定向回归中。

来源权限审查复现了“列表/详情已拒绝，但项目摘要/仪表盘仍返回状态、评分或发现计数”的不一致。已将可见报告类型策略统一应用到来源权限、报告元数据 SQL 过滤与指标 SQL 过滤，并新增 `security_scan`、`fullchain_audit` 和 `pentest` 的拒绝测试。更广的 6 文件、304 项报告/权限回归通过；新代码尚待最终固定 SHA 的 CI。

## 本轮之前的候选基线（历史记录）

以下数据来自本轮增量修改之前的候选基线，不代表当前增量树或最终发布 SHA 的全量测试：

- 后端：7442 passed、8 skipped，344.41 秒；1452 条弃用/收集类警告。
- 前端：151 个测试文件、2002 项通过；ESLint、`vue-tsc` 与 Vite 构建通过。
- 部署脚本回归此前有通过记录；本轮 `bash deploy/tests/test_scripts.sh` 复测也通过，包含 33 项 release binding 与部署故障注入矩阵。

当时的候选 `3203ba31d029235aaa05f03cdf80a3d5422f03b7` 已通过其对应的完整 CI 与 CodeQL；这只是历史基线，与当前 4.1.6 代码候选不同。该阶段曾因 SSH 公钥认证拒绝而未部署；当前生产与部署状态以文末 4.1.6 回归记录为准。

## v4.1.6 候选最终回归（2026-10-18）

候选提交：`ea13db0359e9db285d5b41da3ef3b1a3d924e731`，版本 `4.1.6`。本节结果取代上面 v4.1.4/v4.1.2 候选的“最终”表述；此前 CI/CodeQL SHA 不覆盖当前候选，不作为此版本的验证证据。

| 检查 | 命令或方式 | 结果 |
|---|---|---|
| 后端完整套件 | `backend/.venv/bin/python -m pytest -q --tb=short`，工作目录 `backend/` | 7,362 passed、6 skipped、0 failed，84% 总覆盖率，727.74 秒，退出码 0；6 项为本地 Redis 未配置的集成测试 |
| 报告领域与来源一致性 | `pytest -q tests/unit/services/test_report_dashboard_source_facts.py tests/unit/services/test_domain_report_export_closure.py --no-cov` | 155 passed，14.59 秒，退出码 0；覆盖首轮全量发现的两个失败点及缺失正文新回归 |
| 改动 Python 静态检查 | Ruff 检查改动服务和测试文件 | All checks passed |
| 前端完整测试 | `npm test`，工作目录 `frontend/` | 153 个测试文件、2,022 项通过 |
| 前端 lint 与构建 | `npm run lint`、`npm run build` | 通过；Vite 构建 3,758 个模块 |
| 浏览器端到端 | Playwright 完整套件 | 62 passed、10 skipped |
| 部署脚本回归 | `bash deploy/tests/test_scripts.sh` | release binding、故障注入、shell 与运维检查通过 |
| Git 差异格式检查 | `git diff --check` | 通过 |
| 部署环境预检 | `DEPLOY_ENV_FILE=<受保护本机环境文件> ./deploy.sh all --revision ea13db0359e9db285d5b41da3ef3b1a3d924e731` | 在 `MYSQL_ROOT_PASSWORD` 长度门禁失败并退出；发生在备份、构建、迁移和容器切换之前 |
| 生产健康/就绪 | HTTPS GET `/healthz` 与 `/readyz` | 均为 ready/ok，生产仍是 `4.1.5` / `9986f7fb6876bccdf420bfbc3809e11f4945c2c6` |

本次部署失败是安全预检阻断，不是应用部署失败；没有发生生产数据库或容器状态更改。未创建 GitHub PR 或推送，用户要求 GitHub 报错不阻止本地验证与生产部署。

证据留存说明：测试数量和命令来自本轮执行工具的真实终端回执；本目录未归档 pytest/JUnit、Vitest 或 Playwright 原始日志文件。本节对照验收表、版本文件和提交 SHA 做了独立一致性复核，但没有原始机器报告可供离线重新统计。
