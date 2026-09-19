---
标题: Prism 继续优化机会审计验收
类型: 项目验收清单
状态: v3.9.12 发布门禁完成，生产数据迁移待证据
更新日期: 2026-09-20
观察时间: 2026-09-20
来源: 代码静态核验、生产 3.9.9 验收记录和本目录任务拆分
置信度: 高
tags:
- 主题/项目管理
- 类型/验收
- 状态/实施待执行
---
# Prism 继续优化机会审计验收

| 编号 | 验收标准 | 当前判定 | 证据/缺口 |
| --- | --- | --- | --- |
| AC-O01 | 管理端历史只有一个可写事实源 | 通过 | 旧 POST/GET 接口返回 410；历史 GET 未找到时返回空集合而非创建会话 |
| AC-O02 | GET/列表接口无业务写入 | 通过 | `list_agents`、会话列表、收件箱 GET、profile/forum/feedback GET 均纯读；写入改为显式 POST |
| AC-O03 | 同账号跨 surface 活动会话硬上限 10 | 通过 | 用户行 `FOR UPDATE`；优先归档最旧空闲；全占用时第 11 条 409；054 迁移收敛存量超限会话且不删消息 |
| AC-O04 | 归档会话按账号检索/恢复 | 通过 | `/agent-mesh/conversations?status=archived`、`/restore`；查询固定 `user_id`；前端活动/已归档页签 |
| AC-O05 | 支持中心唯一导航/能力事实源 | 通过 | maintenance/feedback 能力和页面指南全部指向 `/support` |
| AC-O06 | RBAC 解析集中且异常 fail-closed | 通过 | 旧角色字段与关联表不一致、账号禁用、非真实 Session/用户对象都拒绝授权 |
| AC-O07 | 自定义 Agent 目录不泄漏内部所有者 | 通过 | 仅 enabled + published，目录不返回 `owner_id`，派发同样调用可用目录判定 |
| AC-O08 | 请求模型具备统一输入边界 | 通过（本轮范围） | 覆盖 Feedback/Maintenance/Agent Studio/Mesh/Responses/Pentest；拒绝 extra、控制字符、过大/过深 JSON |
| AC-O09 | ORM 与初始化 SQL 一致 | 通过 | `password VARCHAR(60)` 与 ORM bcrypt 存储对齐；合同测试通过 |
| AC-O10 | DeepSeek 截断可恢复 | 部分通过 | `finish_reason=length` 保留部分结果并在配置上限内加倍输出预算重试 1 次；供应商真实 1m 未获回执 |
| AC-O11 | 无会话列表 N+1，跨 worker 确认幂等 | 通过 | 最新运行态改为一次批量查询；OpsExecution 使用 DB 唯一键/行锁/恢复 |
| AC-O12 | 平台上传 CodeQL 与 Codex Security 有真实执行证据 | 阻塞/独立依赖 | 仍缺 CodeQL CLI/许可、隔离 worker 和受管只读扫描权限；不伪造完成证据 |
| AC-O13 | 归档与运行启动不存在 TOCTOU 隐藏运行 | 通过 | 归档与 checkpoint create/claim 共享会话行锁；非 active 会话拒绝新建或恢复运行 |
| AC-O14 | 权限验收不制造第四种业务角色 | 通过 | 生产准备脚本只引用内置 `user`，不创建角色、不改固定角色权限；测试账号无权限样本不绑定角色 |

## 本轮验证边界

- Backend：正确加载 `backend/pyproject.toml` 后 `4252 passed, 5 skipped`，覆盖率总计 79%。
- 前端：`108` 个测试文件、`1225` 个用例全通过；ESLint 和生产 build 通过。
- 历史证据合同：`279 passed`；9 月 8 日 content-addressed 生产 runner SHA 已恢复为 `82b05c...3908c`，新增字段在适配层校验。
- 发布脚本：`19` 项发布绑定检查、全部故障注入回滚分支和 `15` 个运维执行器测试通过。
- Ruff：本轮变更 Python 文件全部通过；`git diff --check` 通过；变更中未写入用户提供的账号密码或手机号。
- 生产状态：`v3.9.11 / 72c82a8c7657...` 已通过备份恢复、Alembic、health/ready、HTTPS 与 `ops-check`。发布后发现历史账号存量会话未自动收敛，已新增可回滚的 054 归档迁移，目标版本为 `v3.9.12`。
