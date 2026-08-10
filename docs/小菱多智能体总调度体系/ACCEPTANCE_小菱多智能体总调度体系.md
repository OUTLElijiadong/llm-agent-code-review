# ACCEPTANCE - 小菱多智能体总调度体系

> 阶段：Assess
> 日期：2026-08-10
> 结论：通过

## 1. 本地质量门禁

| 检查 | 结果 | 证据 |
|---|---|---|
| 后端全量 | 通过 | `1625 passed, 3 warnings`，覆盖率 76% |
| 前端全量 | 通过 | 27 个文件、162 项测试通过 |
| 前端静态检查 | 通过 | `npm run lint`、`vue-tsc`、`vite build` |
| 本任务后端静态检查 | 通过 | Agent Mesh/Responses 相关 Python 文件 Ruff、`compileall` 通过 |
| 迁移 | 通过 | `030_agent_mesh`，SQLite 单测及生产 MySQL 升级通过 |

全仓 Ruff 仍有 10 项既有问题，集中在 `test_case_generator_agent.py`、`project_service.py`、`project_source_revision_service.py`、`sandbox_service.py`，不属于本任务改动，记录到 TODO。

## 2. 生产发布

- 地址：`https://lijiadong.cn`
- 服务器：`81.70.251.90:/opt/code-review`，分支 `deploy-security-monitor`
- 发布源码：`0b70d5d0af9f5d0e74b6f2df9ecf18417200059a`
- Backend 镜像/源码：`prism-backend:764d151fbc9c01fd0bfced613d2f2e0609394422`
- Frontend 镜像/源码：`prism-frontend:0b70d5d0af9f5d0e74b6f2df9ecf18417200059a`
- Alembic：`030 (head)`；`/healthz=200`、`/readyz=200`、HTTP 到 HTTPS `308`
- `cr_backend/cr_frontend/cr_mysql/cr_redis/cr_clamav` 均 `healthy`，重启次数为 0
- 数据库备份：`/opt/code-review/backups/code_review_20260810T100808Z_f6379dc1ba02.sql.gz`、`/opt/code-review/backups/code_review_20260810T101129Z_764d151fbc9c.sql.gz`，权限 `0600`
- 生产 `.env` 已存在 DeepSeek 凭证和 `DEEPSEEK_MODEL=deepseek-v4-flash`；密钥未写入仓库、日志或本报告。

## 3. 真实 API 验收

生产数据库独立核验结果：

| 验收项 | 结果 |
|---|---|
| `ListAgents` | 39 个对象：runtime 14、service 18、custom 2、session 5 |
| 严格契约 | 未知字段返回 HTTP 400，错误码 `40002` |
| 管理端跨会话 | 入站 `msg_8e399a833500479ba0ce24fe53bdf178`，trace `trc_final_ui_1786360816`，completed，事件 4 条 |
| 普通用户跨会话 | 入站 `msg_d637dc18141c4058aa4b8aacfa1285af`，trace `trc_user_ui_20260810_6f1c2a`，completed，事件 4 条 |
| Responses 真实执行 | 两条入站均产生 `list_agents`、`send_message` 成功工具记录，运行状态 completed，未插入伪用户消息 |
| 环境模型 | 真实 SSE 收到 `response.completed`；模型为 `deepseek-v4-flash` |

## 4. 真实浏览器验收

- 管理端与普通用户端均在生产 HTTPS 页面打开小菱助手并完成自动收件。
- 两类页面均显示 `receive_message 已完成`，随后真实展示 `list_agents` 和 `send_message` 调用链及最终模型答复。
- Agent 对话链默认折叠；点击后可看到脱敏的 `sent_from/send_to/message_type/subject/payload/trace_id/status`，再次点击恢复折叠。
- 真实截图检查未发现助手面板与仪表盘内容重叠或文字溢出。
- 浏览器自动化接口不提供视口切换方法，未虚报移动端浏览器截图；窄屏规则由组件测试和生产构建门禁覆盖。

## 5. 临时数据清理

普通用户验收账号 ID 87、管理端验收账号 ID 86 已软删除、令牌版本递增、角色撤销；关联 9 个会话已归档。成功消息与事件时间线保留，作为生产验收审计证据，不再可被账号访问。

## 6. 独立复核说明

按任务规范先后派出文档、架构和最终验收子代理；前三次分别因服务端 `429 Too Many Requests` 或上游流断开失败，缩小上下文后重试仍因上游暂不可用中断，均未产出可引用的核验结论。因此不虚报“子代理通过”。主流程另以全量测试、严格 Ruff/compileall、生产 API、数据库独立查询、两个角色真实浏览器和部署状态交叉核验，发现并修正了 runtime/service 数量、健康探针、SendMessage 审批语义和移动端证据四处文档口径问题。
