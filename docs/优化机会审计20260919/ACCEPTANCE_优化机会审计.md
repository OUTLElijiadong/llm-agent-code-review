---
标题: Prism 继续优化机会审计验收
类型: 项目验收清单
状态: v3.9.14 生产验收完成，外部能力待证
更新日期: 2026-09-20
观察时间: 2026-09-20
来源: 代码与测试证据、生产 3.9.14 发布账本、数据库只读复核和浏览器验收
置信度: 高
tags:
- 主题/项目管理
- 类型/验收
- 状态/审计完成
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
| AC-O15 | 存量会话标题与重复空会话收敛 | 通过 | 055 回填 27 个标题并归档 9 个重复空会话；056 再扫描全部历史 run，补齐 2 个被“最新 run 无用户文本”遮挡的标题；未删除消息、运行或审计数据；迁移状态可回滚 |

## 本轮验证边界

- Backend：正确加载 `backend/pyproject.toml` 后 `4260 passed, 5 skipped`，覆盖率总计 79%。
- 前端：`108` 个测试文件、`1225` 个用例全通过；ESLint 和生产 build 通过。
- 历史证据合同：`279 passed`；9 月 8 日 content-addressed 生产 runner SHA 已恢复为 `82b05c...3908c`，新增字段在适配层校验。
- 发布脚本：`19` 项发布绑定检查、全部故障注入回滚分支和 `15` 个运维执行器测试通过。
- Ruff：本轮变更 Python 文件全部通过；`git diff --check` 通过；变更中未写入用户提供的账号密码或手机号。
- 生产版本：`v3.9.14 / ccc9284af7d9ddf71d47e022a667a68eef4fbe18`；前后端镜像、Git HEAD、`healthz/readyz` 与版本一致，Alembic 为 `056_older_run_titles`。
- 备份恢复：发布前备份 `code_review_20260920T072208Z_ccc9284af7d9.sql.gz` 已在隔离库恢复，恢复后 99 张表；发布后 `ops-check` 全绿。
- 生产会话数据：活动 66、归档 631、消息 850、Responses 运行 800；无账号超过 10 条活动会话，单账号最大 10；运行/审批占用会话 6 条且全部仍为 active。
- 生产迁移数据：054 归档 83 条；055 共变更 36 条（标题 27、状态 9）；056 从较早 run 回填会话 81、89 共 2 条标题。剩余占位标题 328 条（active 10、archived 318），扫描所有历史 run 后“可提取用户内容但仍为占位”为 0；同账号同 surface 的真空白活动占位最多 1 条。
- 生产角色数据：活跃角色只有 `user/reviewer/admin/super_admin`；21 个活跃用户均符合固定角色集合，不存在活跃自定义业务角色或旧字段/关联表不一致。
- 生产授权：`reviewer/admin/super_admin` 均具备 `agent_asset:create/update_own/test/submit`、`skill_asset:create/update_own` 和 `pentest:authorize`；普通 `user` 不具备这 7 项权限。
- 浏览器：强制刷新后登录态生产工作台显示 `v3.9.14 · PRISM`，合并后的唯一入口为 `Agent 工作台`、`安全与规则`、`支持中心`；当前任务已取得页面截图，但逐角色逐控件矩阵仍不能由单账号截图替代。
- 独立复核：子代理重新执行了 3.9.14 生产只读数据、权限矩阵与 056 全历史 run 扫描，结果与主核验一致，无功能、数据或 RBAC 阻断。
