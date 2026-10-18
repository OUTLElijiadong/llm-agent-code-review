# 报告生成与小菱停止修复测试证据

更新日期：2026-10-18
正式生产版本：`4.1.6`
正式发布 SHA：`e9e02858342bc4cb33119b070c09cfeb502493e6`

> 本文保留逐轮测试与部署前状态的历史记录。文中“当前候选”“生产仍为 4.1.5”“SSH 拒绝”“尚未部署”等描述均指各自记录当时的状态，不代表当前生产。当前正式发布和未完成业务验收以 [[../稳定部署与生产验收20261018/ACCEPTANCE_稳定部署与生产验收]] 为准。

历史候选工作树：`/Users/li/.codex/worktrees/prism-v4-1-5-release/基于大模型智能体的代码审查平台`
历史候选分支：`codex/prism-v4-1-5-release`

下方记录按时间保留各阶段测试快照。4.1.6 发布前本地回归以“报告与停止修复增量复验”为准；当前生产发布和生产健康检查以本文开头链接的正式验收记录为准。`3203ba31d029235aaa05f03cdf80a3d5422f03b7` 的 CI/CodeQL 不覆盖正式发布 SHA，因此不作为正式版本的 CI 证据。

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

## v4.1.6 候选基线回归（2026-10-18）

候选基线提交：`ea13db0359e9db285d5b41da3ef3b1a3d924e731`，版本 `4.1.6`。本节结果取代上面 v4.1.4/v4.1.2 候选的“最终”表述；此前 CI/CodeQL SHA 不覆盖当前候选，不作为此版本的验证证据。其后的管理员页面增量修复及当前完整 SHA 见后续章节。

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
| 生产部署目标只读连通检查 | SSH 连接项目发布手册中的 `/opt/code-review` 主机，BatchMode | 返回 `Permission denied (publickey,...)`；远端未执行命令、未读服务器环境文件 |
| 本机容器目标 | `docker context show`、Docker context/socket 检查 | 当前 context 为 `desktop-linux`，本机 socket 不存在；没有可用本地 Docker daemon。项目文档要求在 Linux 生产机发布 |

本次部署失败是安全预检阻断，不是应用部署失败；没有发生生产数据库或容器状态更改。未创建 GitHub PR 或推送，用户要求 GitHub 报错不阻止本地验证与生产部署。

2026-10-18 的额外只读检查发现，候选工作树未包含 `deploy/.env`；主工作区中的受保护文件权限为 `600`，本机解析出的 root 与应用数据库密码长度分别为 7 与 14（未输出密码本身）。这些本机值不能证明生产主机配置相同。当前 SSH 公钥认证拒绝，故尚未读取服务器发布账本、磁盘空间、数据库凭据或 Docker 状态；本机 Docker context 也未连接到生产 Linux 主机。下一次部署前必须先恢复受授权的 SSH 发布访问，并在服务器本地运行正式发布脚本。

证据留存说明：测试数量和命令来自本轮执行工具的真实终端回执；本目录未归档 pytest/JUnit、Vitest 或 Playwright 原始日志文件。本节对照验收表、版本文件和提交 SHA 做了独立一致性复核，但没有原始机器报告可供离线重新统计。

## 2026-10-18 本轮增量复验

候选 SHA `ea13db0359e9db285d5b41da3ef3b1a3d924e731` 是此前基线；后续管理员页面修复与当前报告/停止修复均不应套用该 SHA 作为完整增量的提交身份。

| 检查 | 命令或方式 | 结果 |
|---|---|---|
| 报告、停止恢复与权限后端回归 | `/tmp/prism-acceptance-20261018/bin/python -m pytest -q --tb=short tests/unit/agents/test_roundtable_followup.py tests/unit/agents/test_roundtable_lifecycle.py tests/unit/agents/test_roundtable_restart_recovery.py tests/test_report_object_authorization.py tests/test_review_task_access_errors.py tests/test_dashboard_private_report_scope.py tests/unit/services/test_domain_report_export_closure.py`（工作目录 `backend/`；临时 Python 3.11 环境） | 7 个测试文件、324 passed、2 warnings，41.90 秒，退出码 0；警告为 `crypt` 与 Starlette TestClient 弃用提示 |
| 管理员 Token、登录光球、依赖星图与角色路由前端回归 | `npm test -- --run src/views/admin/AdminOverview.test.ts src/views/auth/Login.test.ts src/components/ai/AgentTeamTrace.test.ts src/router/guards.test.ts`（工作目录 `frontend/`） | 4 个测试文件、82 passed，退出码 0 |
| 差异格式检查 | `git diff --check` | 通过 |
| 生产 Safari 页面状态 | CUA 连接 Safari，检查登录页 DOM 并捕获当前截图 | 当前生产页面构建版本显示为 `4.1.5`；访问 `/dashboard` 被重定向到登录页，提示账号已在另一设备登录、当前设备已下线。Safari 密码自动填充要求用户解锁钥匙串，因此没有有效会话，未继续点击停止、管理员导航或启动模型任务 |
| CNVD/CNNVD 官方数据接入条件 | 查阅 CNNVD 官方首页文档清单及官方兼容性白皮书 | 官方列出 XML 数据描述规范与获取接口文档；官方白皮书说明获取 XML 数据需登录站点下载。当前工作区没有授权数据文件或 CNNVD 登录会话，因此未实现或声称已完成逐条官方匹配。 |

本轮浏览器观察不构成三角色业务验收；82 项前端测试与 324 项后端测试也不能替代生产账号交互。截图在本轮工具输出中展示，但当前 Computer Use 接口没有提供将截图保存到仓库验收目录的受支持操作。候选部署仍受数据库凭据强度门禁阻止，生产保持 v4.1.5。

## 2026-10-18 管理员页面增量修复

增量提交：`dfea354b5c5d1ea1971eb052512743ce97e5ab4f`，版本仍为 `4.1.6`，没有额外增加版本号。源文件：`frontend/src/components/ai/ProactivePageGuide.vue`、`frontend/src/components/layout/AppLayout.vue`、`frontend/src/components/admin/AdminLayout.vue`、`frontend/src/components/security/AutomaticBlockingPanel.vue` 及对应三份组件测试。

| 检查 | 命令/方式 | 结果 |
|---|---|---|
| 引导卡片、管理员布局、安全策略定向回归 | `npm exec vitest run src/components/ai/ProactivePageGuide.test.ts src/components/admin/AdminLayout.test.ts src/components/security/AutomaticBlockingPanel.test.ts`（工作目录 `frontend/`） | 3 个测试文件、43 项通过 |
| 用户主布局回归 | `npm exec vitest run src/components/layout/AppLayout.test.ts`（工作目录 `frontend/`） | 1 个测试文件、6 项通过 |
| 前端完整测试套件 | `npm test -- --reporter=dot`（工作目录 `frontend/`） | 153 个测试文件、2,023 项通过，45.58 秒，退出码 0；Vitest 输出包含现有组件 stub/router 警告，不影响测试结果 |
| 改动文件 ESLint | `npm exec eslint src/components/ai/ProactivePageGuide.vue src/components/layout/AppLayout.vue src/components/admin/AdminLayout.vue src/components/security/AutomaticBlockingPanel.vue src/components/ai/ProactivePageGuide.test.ts src/components/admin/AdminLayout.test.ts src/components/security/AutomaticBlockingPanel.test.ts` | 退出码 0 |
| 前端类型检查与生产构建 | `npm run build`（工作目录 `frontend/`） | `vue-tsc` 通过；Vite 构建 3,758 个模块成功 |
| 补丁空白检查 | `git diff --check` | 退出码 0 |

生产 v4.1.5 只读浏览确认：治理中心审批、Agent 发布、执行请求、规则、自进化、经验记忆、黄金回归集、反馈、知识、Skill 子页可打开；安全中心概览、事件、策略、溯源、诱捕层可打开。候选修复前，固定引导卡片覆盖一部分治理卡片操作入口；横向裁剪风险由 `overflow-x:hidden` 布局确认。安全策略旧文案“规则封禁最长 15 分钟”与代码实现不符，服务端规则/自动延长硬上限为 3600 秒，小菱每次研判封禁上限为 120 秒，已在候选 UI 文案分别注明。没有触发会修改安全策略的保存按钮或模型任务。

运行数据观察只作历史边界：Skill 页面最近 30 次记录显示最近运行时间为 2026-08-27，不能代表当前自动运行；2026-10-18 安全中心概览显示最新数据源任务已成功，但事件历史中仍有较早的“进行中”记录。该观察不是对每条任务实际状态或当前调度健康度的定论。

独立复核结果：另一代理重新读取当前 HEAD、版本与四份交付文档，并重新运行四个目标组件测试（49/49）、完整前端测试（153 个文件、2,023/2,023）及类型检查/生产构建（3,758 模块），均与本节记录一致。原始机器日志没有归档；此复核不能代替生产三角色与真实报告验收。

## 2026-10-18 报告与停止修复增量复验

本节是当前 4.1.6 候选的最终本地回归记录。HEAD `dfea354b5c5d1ea1971eb052512743ce97e5ab4f` 是其基线，不包含本节下列改动；当前完整候选以工作分支 HEAD 为准。

| 检查 | 命令或方式 | 结果 |
|---|---|---|
| 后端完整套件 | `/tmp/prism-acceptance-20261018/bin/python -m pytest -q --tb=short`（工作目录 `backend/`） | 最新运行收集 7,382 项；7,376 passed、6 skipped、0 failed，84% 覆盖率，533.48 秒，退出码 0；5 条警告。6 项为本机未配置 Redis 的集成测试 |
| 报告 API、导出与一致性定向回归 | `pytest -q tests/test_report_exporter.py tests/test_report_api.py tests/test_c28_report_consistency.py` | 独立复核运行 161 passed、2 条依赖弃用警告；另外本轮 7 个新增/修正回归用例通过。覆盖中文状态、空值/未知状态兜底、简洁版问题处理状态、排版清理、覆盖范围边界与导出一致性 |
| 安全扫描部分结果 | `pytest -q tests/unit/api/test_security_partial_results.py` 及安全相关回归 | 新增部分结果用例 7 passed；失败扫描仍保留已得到的发现与执行状态，避免报告丢失有效证据 |
| 停止按钮交互 | `npm exec vitest run src/components/ai/AgentChatDrawer.test.ts` | 62 passed；覆盖首个服务端响应前生成 run id、取消未确认时保留 SSE/运行状态与重试入口、迟到团队回执不自动重启 |
| 报告详情交互 | `npm exec vitest run src/views/report/ReportDetail.test.ts` | 42 passed；覆盖扫描范围、未扫描/排除/失败文件展示与详情清晰度 |
| 前端完整测试 | `npm test -- --reporter=dot`（工作目录 `frontend/`） | 153 个测试文件、2,025 passed，44.05 秒，退出码 0 |
| 前端静态检查与生产构建 | `npm run lint` 与 `npm run build`（工作目录 `frontend/`） | ESLint、`vue-tsc` 通过；Vite 构建 3,758 个模块成功，58.40 秒 |
| 浏览器端到端 | Playwright 完整套件 | 62 passed、10 skipped，共 72 项，约 3 分钟，退出码 0 |
| 发布脚本故障注入 | `bash deploy/tests/test_scripts.sh` | release binding、备份/恢复/部署/回滚故障注入矩阵、shell 与 ops 检查通过 |
| Python 静态检查与补丁格式 | 改动 Python 文件 Ruff；`git diff --check` | 均通过 |

首次全套件曾发现一项 API 测试仍期待原始英文人工复核状态；报告现按中文标签展示，已修正断言。简洁版 HTML 增加处理状态列；新增 5 个未知/空值回退用例。最新定向报告 API、导出与一致性测试 161 项通过，7 个新增/修正用例通过；最新完整后端套件 7,376 项通过、6 项 Redis 测试跳过。上述均为本地自动化/合成测试；不代表候选已经部署、真实账号报告已生成，或上游模型计费已停止。

简洁版仍保留为摘要格式，不展示详细版中的证据质量、人工复核和冲突字段；这些信息在详细版与合规版中展示。未知和缺失状态以中文说明，不伪装为成功或修复。

最终只读生产 GET `/healthz` 与 `/readyz` 返回 `4.1.5` / `9986f7fb6876bccdf420bfbc3809e11f4945c2c6`。发布机 SSH 公钥认证被拒绝，本地发布预检在修改生产前受数据库凭据策略门禁阻止；没有创建备份、迁移、切换容器或执行生产写操作。真实账号逐页验收和每类真实 AI 报告仍待 Safari 登录与候选部署后进行。
# 历史验证记录说明

本文件保留了 2026-10-18 部署前的逐次测试与阻断记录；其中“生产仍为 4.1.5”“SSH 拒绝”“尚未部署”等内容是当时状态，已被后续正式发布取代。当前生产版本为 4.1.6，SHA `e9e02858342bc4cb33119b070c09cfeb502493e6`。本次最终部署与生产回执以 [[../稳定部署与生产验收20261018/ACCEPTANCE_稳定部署与生产验收]] 为准；本地完整测试结果以该验收记录为准。
