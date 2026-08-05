# ACCEPTANCE：生产 Agent 审批失败回退与 tool_choice 修复

| # | 验收项 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | thinking 模型 non_thinking_repair 不携带 tool_choice | ✅ | 单测 `test_tool_request_options_never_sends_tool_choice_for_thinking_models` 通过；容器内验证 `_tool_request_options("deepseek-v4-flash",1,non_thinking_repair=True)=={}` |
| 2 | failed 检查点 retry 可恢复；非终态拒绝 | ✅ | `test_retry_recovers_failed_checkpoint`、`test_retry_rejects_non_recoverable_status` 通过 |
| 3 | 已应用审批的 failed 运行 approve 幂等续跑 | ✅ | `test_approve_after_terminal_failure_with_evidence_resumes` 通过；run_ede09 三个尾部 call 均有证据 |
| 4 | API 契约接受 retry | ✅ | `test_api_request_accepts_retry_action_and_dispatches_resume` 通过；OpenAPI 契约检查 PASS |
| 5 | 前端管理/成员 Agent 有重试入口 | ✅ | 镜像 assets 含“重试运行”（2 个 chunk）；旧 stepStreamOpen 已移除 |
| 6 | 后端测试 | ✅ | runtime 27 passed；integration+api+admin 115 passed；ruff 全绿 |
| 7 | 前端 lint/test/build | ✅ | eslint 通过；24 tests passed；vue-tsc+vite build 成功 |
| 8 | 生产部署健康 | ✅ | 5 容器 healthy；/healthz、/readyz 200；nginx -t OK；HTTPS 200 |
| 9 | 生产数据可恢复性 | ✅ | run_ede09：failed、pending=null、error 含 tool_choice、尾部调用有证据，可 retry |
| 10 | 回滚能力 | ✅ | 旧镜像 docroot-fix-08051715 / fold-block-4fd17fb 保留；源码备份 .bak-agent-retry-20260805-1735 |

待子代理独立复核结果补充确认。
