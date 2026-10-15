# v4.0.103 候选本地验证（2026-10-15）

候选工作树：`codex/sandbox-php-context`。下列记录对应当前候选源码；本地结果不能替代生产发布、普通 Safari 点击或真实模型服务端到端验收。

| 范围 | 命令 | 实际结果 |
|---|---|---|
| 后端定向回归 | `backend/.venv/bin/python -m pytest backend/tests/unit/services/test_agent_team_terminal_notification.py backend/tests/unit/services/test_agent_team_service.py backend/tests/unit/services/test_agent_team_dispatcher.py backend/tests/unit/services/test_agent_mesh_service.py backend/tests/unit/services/test_agent_mesh_dispatcher.py backend/tests/unit/services/test_agent_mesh_run_state_projection.py backend/tests/unit/services/test_site_completion_guard_recheck.py backend/tests/unit/services/test_forum_ordering.py backend/tests/unit/services/test_background_task_recovery.py -q --no-cov` | **365 passed**, 181 warnings，20.17s。警告为已有 `crypt`、`datetime.utcnow()` 与 Starlette 测试客户端弃用提示。不是全后端套件。 |
| Python 规范 | `backend/.venv/bin/ruff check backend/app/core/config.py backend/app/main.py backend/app/services/background_task_recovery.py backend/tests/unit/services/test_background_task_recovery.py` | All checks passed。 |
| 前端测试 | `pnpm vitest run`（`frontend/`） | **147 test files passed，1935 tests passed**。 |
| 前端 lint | `pnpm lint`（`frontend/`） | exit 0。 |
| 前端生产构建 | `pnpm build`（`frontend/`） | `vue-tsc` 与 Vite 构建通过；3749 modules transformed，Vite built in 43.45s。 |

新增场景：两个 Mesh 回执立即重试都失败后，同一进程的下一次周期扫描可收敛终态回执；团队终态回执到达后前端自动启动小菱回复并显示结果，无需用户再点发送或续跑。真实生产代码提交、第三方模型可用性、浏览器离线后服务端代替前端生成自然语言总结、375/390px 实际点击与遮挡，均不在以上本地测试证据内。
