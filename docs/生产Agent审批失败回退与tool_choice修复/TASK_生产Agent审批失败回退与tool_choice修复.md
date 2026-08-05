# TASK：生产 Agent 审批失败回退与 tool_choice 修复

| 子任务 | 输入 | 输出 | 验收 |
| --- | --- | --- | --- |
| T1 后端运行时修复 | deepseek_responses_runtime.py（服务器版本为基） | 修复后文件 + 单测 | thinking 模型 non_thinking_repair 不携带 tool_choice；retry/幂等续跑可用 |
| T2 服务层 retry 分支 | agent_responses_service.py（服务器版本为基） | resume 支持 retry | resume(action=retry) 调用 runtime.retry |
| T3 API 契约 | api/v1/agent_responses.py（两端一致） | action Literal 增加 retry | openapi/契约测试通过 |
| T4 前端管理 agent | AdminCopilot.vue | 失败运行显示“重试运行” | 点击后 action=retry 发起 SSE |
| T5 前端成员 agent | AgentChatDrawer.vue | 同上 | 同上 |
| T6 本地验证 | 上述改动 | pytest + ruff + 前端 lint/test/build 通过 | 全绿 |
| T7 生产部署 | /opt/prism-current 备份 | 重建 backend/frontend 镜像 + 重启 | /healthz 200，容器 healthy，旧镜像保留 |
| T8 生产验证 | 生产 DB + API | run_ede09 可恢复路径、日志无 400 | 复核通过 |

依赖：T1→T2→T3；T1/T4/T5 可并行；T6 依赖全部；T7 依赖 T6；T8 依赖 T7。
