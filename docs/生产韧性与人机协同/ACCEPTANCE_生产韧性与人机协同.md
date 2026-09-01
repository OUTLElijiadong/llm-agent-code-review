# 生产韧性与人机协同：验收记录

## 验收结论

生产版本 `3.7.4` 已于 `2026-09-01T06:28:54Z` 发布，运行提交为 `c1fc546de597d6258ca29945dd824afb9744265d`。关键服务、备份恢复、迁移、HTTPS、错误契约、人机交互和自然调度均已通过；生产当前有磁盘 88% 和 FlyTrap 外部上报超时两项非阻断降级，均具备机器可读状态、明确人工动作和后续恢复入口，没有阻塞性失败。

## 需求验收矩阵

| 验收项 | 结果 | 实际证据 |
| --- | --- | --- |
| 非关键错误不堵死 | 通过 | 生产巡检返回 `status=degraded`、`can_continue=true`、退出 0；磁盘 88% 未被误判为全面失败。 |
| 关键错误仍安全阻断 | 通过 | 容器、备份、迁移、HTTPS 和临界资源失败用例保持 `error`/退出 1。 |
| 结构化报错 | 通过 | 未登录请求返回 401，并包含 `code`、`message`、`request_id`、`retryable=false`、`next_action=请重新登录后再试`。 |
| Agent 失败可恢复 | 通过 | Responses 取消竞态、错误卡片、重试、重新提问、审批再确认和团队状态保留均有后端/前端回归用例。 |
| 人工接管 | 通过 | 巡检动作携带 `requires_human=true`；Clarify 过期不重放旧 ID，改为原问题预填并等待人确认。 |
| 孤儿任务收敛 | 通过 | 启动清扫 12 条跨进程遗留 `running` 记录，全部写入“可由管理员重新运行”；清扫后 `running_total=0`。 |
| 安全巡检部分失败 | 通过 | 逐动作记录完成/失败来源；部分来源失败可继续，所有来源失败才判为致命失败。 |
| 秒级沙箱心跳 | 通过 | `interval@30s` 失败测试先复现；生产启动注册 8 个任务，job `35` 连续每 30 秒自然执行成功。 |
| systemd 稳定路径 | 通过 | 运维执行器、巡检、备份、备份校验定时器均使用稳定部署目录并处于 active。 |
| FlyTrap 状态与回退 | 通过 | 本地 agent/sync 均 active；上游超时返回 `degraded=true`、`can_continue=true`、人工动作并保留持久队列，未把运维 JSON 误计为攻击。 |
| 人员可见告警 | 通过 | 真实巡检创建 warning 集成告警 `68`；恢复后同一指纹告警自动关闭的测试通过。 |

## 本地验证

- 后端：`2113 passed, 1 warning in 76.60s`。唯一警告是既有测试类带 `__init__` 的 Pytest 收集提示，不影响本次功能。
- 前端：64 个测试文件、`384 passed`；存在测试环境缺少 Router/Element Plus 注入的既有 Vue warning，无失败。
- 新增定向测试：FlyTrap 运维解析/健康 3 项、安全巡检降级/恢复 2 项全部通过；测试均先失败后实现。
- 代码规范：本次变更文件 Ruff、ESLint、`git diff --check` 全部通过；全仓 Ruff 仍有 150 个未触及的历史问题，未越界重写旧代码。
- 构建：前端 `3682` 个模块完成生产构建。
- 部署脚本：`deploy/tests/test_scripts.sh` 通过。

## 生产发布证据

### 版本与回退

- 当前版本：`3.7.4`。
- 当前提交：`c1fc546de597d6258ca29945dd824afb9744265d`。
- 上一可回退应用版本：`8d97e91ea0de6ec9a58cbcb7ad1e1a0c6b452cdc`。
- Alembic 当前与单头：`045_skill_asset_user_grant`。
- 服务器 Git 工作树：干净。

### 发布前备份

- 文件：`/opt/code-review/backups/code_review_20260901T062426Z_c1fc546de597.sql.gz`。
- 大小：`399079048` 字节。
- SHA-256：`c07488dd4b71350cc9e06c9965436e0e85ff6aca57d73095ace2e41961a3232d`。
- gzip：通过。
- 独立恢复：通过，89 张表，Alembic `045_skill_asset_user_grant`。

### 服务与公网

- `cr_backend`、`cr_frontend`、`cr_mysql`、`cr_redis`、`cr_clamav` 健康；`cr_embedding` 正常运行。
- `/healthz`：HTTP 200，`status=ok`，版本与提交匹配。
- `/readyz`：HTTP 200，`status=ready`，版本与提交匹配。
- HTTP 跳转 HTTPS 为 308；HSTS、`X-Content-Type-Options`、`X-Frame-Options` 等响应头存在。
- `prism-ops-executor` 已在发布后重启，实际入口为 `/opt/code-review/deploy/prism_ops_executor.py`，新 Socket 已就绪。
- FlyTrap 到 `42.194.238.178:8443` 的 gRPC 和同步仍超时；本地 agent/sync 都是 active，持久队列持续保留并重试。该故障已被正式安全巡检识别为可继续降级，而非被掩盖或误报为攻击。

### 自然调度

- 运维巡检 run `38738`：数据库状态 `success`；业务状态 `degraded`、`can_continue=true`，无阻断检查。
- 安全巡检 run `38841`：数据库运行状态 `success`；业务结果 `success=false`、`partial_failure=true`、`can_continue=true`，`degraded_actions=[flytrap_attack_events]`、`failed_actions=[]`。这表示本轮其余结果可用且允许后续继续，但不会把外部同步故障伪装成全绿。
- 独立只读复核观察到后续 run `38850`：数据库状态仍为 `success`，7 个动作均完成，`fatal_failure=false`，仅 FlyTrap 保持同一非阻断降级；数据库遗留 `running` 记录为 0。
- FlyTrap 健康摘要：agent/sync 均 active，最近上游/同步信号为失败，本地合法攻击事件计数 `0`；管理员告警 `68` 为 `warning/open/integration`，并提示检查上游服务、防火墙和网络路由。
- 沙箱心跳 job `35`：配置 `interval@30s`，run `38729` 起连续成功；最近结果为 `heartbeat=0`、`recovered=0`。
- 启动清扫没有自动重放旧任务副作用，只将中断记录收敛并要求管理员决定是否重新运行。

## 浏览器交互验收

- 当前 `3.7.4` 公网登录页在 `1280x720` 桌面端和 `390x844` 移动端实际渲染，页面滚动宽度分别等于视口宽度，无横向溢出、无控制台错误。
- 登录按钮可操作；空表单提交后账号和密码字段就地标红，并分别提示“请输入用户名”“请输入密码”。
- 失败提示留在当前界面，不依赖短暂提示，也不会让用户在无原因、无动作的状态中等待。

## 未自动处理事项

未执行任何磁盘删除、数据库日志开关变更或旧任务重放。需要人工决定的事项已集中写入 `TODO_生产韧性与人机协同.md`。
