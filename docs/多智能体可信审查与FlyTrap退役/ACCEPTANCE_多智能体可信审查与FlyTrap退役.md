# Acceptance：多智能体可信审查与 FlyTrap 退役

## 验收结论

本次需求已完成代码、迁移、前端、报告、运维退役和生产发布闭环。系统不再把坏格式、未审查大文件或聚合异常伪装为“零问题成功”；单 Agent、单分片或单数据源失败时保留其他有效结果并给出诊断、回退和人工处理入口。

概率模型不存在可证明的永久零误报、零漏报保证。本次验收保证的是：输入守恒、结论可追溯、冲突不静默覆盖、覆盖缺口可见、版本可复验、失败不堵死、人可裁决。

## 需求验收矩阵

| 需求 | 实现与证据 | 结果 |
| --- | --- | --- |
| 输出格式统一 | 严格解析 `issues` 数组，严重度别名统一；混合坏条目隔离，全坏/非数组明确失败 | 通过 |
| 严重度与置信度统一 | `finding-aggregation-v1` 和 `claim-risk-v2` 分离影响等级与可信程度，并按唯一真实来源计权；0..1、1..100 置信度统一映射 | 通过 |
| 冲突消解 | 完全连接聚类；canonical 严重度与置信度来自同一主张；跨档、CWE、证据和异议冲突进入待人审 | 通过 |
| 来源真实性 | 只统计实际 Agent/引擎来源，不再根据模型声明补造确认来源 | 通过 |
| 聚合异常回退 | 聚合器内部异常时逐条保留可用原始主张，标记 `unresolved/pending`，审查继续 | 通过 |
| 模型波动控制 | 提示词温度归零、上下文和聚合版本固定、同输入重复评测、结果签名稳定性门禁 | 通过 |
| 复杂调用上下文 | Python AST 提取函数、嵌套函数、参数、变量、调用、导入、继承；有界关系上下文进入每个模型请求 | 通过 |
| 分片完整性 | 超长定义和单长行继续拆分，单片不超过阈值且源码字符不丢失；语法失败走带诊断的词法回退 | 通过 |
| 超大文件审查 | 安全哨兵取消超过 60000 字符直接跳过逻辑，改为上下文分片；部分失败不伪装全量成功 | 通过 |
| 人工交互 | 问题详情显示风险、证据、冲突和真实来源；支持接受、驳回、要求补证，失败保留页面并可重试 | 通过 |
| FlyTrap 退役 | 代码默认禁用并返回 `retired`；生产服务、同步和续签 timer 已停用且禁止自启，历史资料保留 | 通过 |

## 自动化验证

| 验证项 | 结果 |
| --- | --- |
| 后端全量测试 | `2141 passed, 1 warning`，77.50 秒 |
| 前端全量测试 | 64 个测试文件、`385 passed` |
| 前端生产构建 | 通过 |
| 发布脚本测试 | `15 passed` |
| Python 代码规范 | Ruff 通过 |
| Shell 语法与静态检查 | `bash -n` 与 ShellCheck 通过；仅排除仓库既有、已说明的 `SC1091/SC2016` |
| 补丁完整性 | `git diff --check` 通过 |
| 数据迁移 | `045_skill_asset_user_grant -> 046_finding_aggregation` 升降级测试通过 |

关键回归用例覆盖：非数组输出、部分/全部坏条目、严重度与置信度错配、虚假来源计数、同一真实来源重复不增加风险权重、输入排列稳定性、聚合器异常回退、嵌套函数、继承、跨片调用、超长函数、单长行、AST 失败、超过 60000 字符的源码守恒和部分分片失败时保留其他发现、重复评测不稳定门禁、FlyTrap 退役态和人审权限/状态。

独立复核曾复现一个真实缺陷：同一来源重复 10 次会将风险分从 `62.5` 推高到 `93.18`，而确认来源数仍为 2。修复后计分与来源计数使用同一去重集合，风险算法版本升为 `claim-risk-v2`；正常输入和重复输入均为 `62.5`。

## 生产发布证据

- 公网入口：`https://lijiadong.cn`。
- 版本：`3.8.1`。
- 发布提交：`c8a80228f48b949daabe4520c880a8994540c08b`。
- 发布时间：`2026-09-01T12:24:27Z`。
- 发布目标：后端和前端全部切换到同一提交。
- Alembic：`046_finding_aggregation (head)`。
- `review_issue` 六个新增字段全部存在：`aggregation_version`、`evidence_quality`、`conflict_status`、`human_review_status`、`risk_score`、`aggregation_json`。
- 生产 OpenAPI 模型中存在 `PUT /api/issues/{issue_id}/review-decision`。
- 后端、前端、MySQL、Redis、ClamAV 均为 healthy；Embedding 服务 running。受控清理前的额外备份隔离恢复触发 MySQL 内存上限，MySQL 自动重启 1 次；其他五个容器重启数仍为 0。
- 公网 `/healthz`、`/readyz` 和首页分别返回正常状态、正常状态和 HTTP 200，健康响应中的版本与提交一致。
- 初始发布后至额外备份恢复校验前，精确日志检查没有后端 `ERROR/CRITICAL/Traceback`，没有前端 error/5xx；Nginx 配置检查通过。
- 生产库在 3.8.0 窗口期间的已聚合记录数为 0，无需对业务数据做追溯重算。
- 在生产容器内执行同源重复复现：基线与重复 10 次后风险分均为 `62.5`，`confirmation_count=2`，`risk_scoring_version=claim-risk-v2`。

## 数据与回滚证据

- 发布前数据库备份：`/opt/code-review/backups/code_review_20260901T122011Z_c8a80228f48b.sql.gz`。
- 备份大小：`399484192` 字节，权限 `600`，SHA-256：`321c2a9d4a27d492ccca02967eb5c7d7e7f5d6a1dc58022d70a22217d11e56c3`。
- `gzip -t` 通过；发布流程在隔离数据库中完成恢复验证，共识别 89 张表。
- 显式退役开关写入前的生产环境备份：`/opt/code-review/backups/deploy-env-pre-flytrap-explicit-20260901T123300Z`，权限 `600`。
- 发布元数据同步前的生产环境备份：`/opt/code-review/backups/deploy-env-pre-release-metadata-sync-20260901T125424Z`，权限 `600`。
- 生产 `.env` 中 `APP_RELEASE`、`BACKEND_RELEASE`、`FRONTEND_RELEASE` 已与发布账本同步为当前 SHA，`APP_VERSION=3.8.1`；无参数 Compose 解析也选中当前前后端镜像，避免后续资源同步误用历史版本。
- FlyTrap 退役备份：`/opt/code-review/backups/flytrap-retirement-20260901T111647Z`。
- FlyTrap 备份包含三个运行单元、静态续签 service、同步环境文件和停用前状态；原始 `SHA256SUMS` 与停用后状态哈希全部通过。
- 代码回滚使用 `deploy/rollback.sh` 的上一发布账本；FlyTrap 恢复仅在项目重新启用时执行：

```bash
systemctl enable --now flytrap-agent.service flytrap-sync.service flytrap-agent-cert-renew.timer
```

## 受控清理验收

- 执行命令：`deploy/cleanup.sh --apply`，退出码 `0`。
- 清理前 dry-run 列出 199 个旧发布镜像标签、2 项按 168 小时阈值执行的镜像/构建缓存 prune，不删除发布状态文件。
- 实际释放 `9,972,244,480` 字节，即 `9.29 GiB`；根盘从 89% 降至 84%，可用空间约 31 GB。
- Docker 镜像数从 239 降至 67，构建缓存条目从 483 降至 364；阈值内的近期缓存和受保护镜像保留。
- 当前 `c8a80228f48b949daabe4520c880a8994540c08b` 和上一版 `46a718aeae12431627993ecd285bc1774350206f` 的前后端四个镜像全部存在，运行容器仍引用当前版本。
- 审计目录：`/opt/code-review/backups/controlled-cleanup-20260901T145933Z`，包含清理前后镜像清单、dry-run、apply 日志、发布账本快照和 `SHA256SUMS`；全部哈希复验通过。
- 清理后 `ops-check.sh` 返回 `status=ok / can_continue=true / blocking_checks=[]`，数据库 89 张表且 `user`、`review_task`、`review_issue`、`agent_job_run` 快速检查全部为 `OK`。
- 额外验证创建的 `prism_verify_20260901145022_12237` 临时库已精确删除；2026-08-19 的历史临时库未纳入本次授权，保持未动。
- OOM 期间为 `2026-09-01T14:51:14Z` 至 `14:51:39Z`；后端在数据库恢复前共出现 120 个 MySQL `Connection refused` Traceback。`14:51:40Z` 之后后端错误/Traceback/5xx 和前端严重日志/5xx 均为 0。

## FlyTrap 生产验收

| 项目 | 生产实测 |
| --- | --- |
| `flytrap-agent.service` | `disabled / inactive / MainPID=0` |
| `flytrap-sync.service` | `disabled / inactive / MainPID=0` |
| `flytrap-agent-cert-renew.timer` | `disabled / inactive` |
| `flytrap-agent-cert-renew.service` | `inactive`，保留静态 unit |
| Prism 配置 | 后端 `SECURITY_FLYTRAP_ENABLED=false`，运维执行器 `PRISM_FLYTRAP_ENABLED=false`，生产 `.env` 已明示写入 |
| 巡检返回 | `enabled=false / status=retired / degraded=false / can_continue=true` |
| 旧告警 | `#68` 已于 `2026-09-01 11:25:38` 解决，原因明确为“集成已按退役计划停用” |
| 后续自动巡检 | 最近 5 轮均 success，`can_continue=true`，无失败和降级动作；发布后已覆盖 `12:28`、`12:33` UTC |

## 浏览器验收

- 桌面：`1440 x 900`，登录页无横向溢出、遮挡或控制台错误。
- 移动端：`390 x 844`，输入框和按钮尺寸稳定，无横向溢出或文本越界。
- 空表单提交后，账号和密码字段分别显示明确中文错误，页面不跳转、不白屏，控制台无错误。
- 生产浏览器未使用真实账号修改业务数据；问题人审抽屉、状态更新和错误回退由前端组件测试与后端权限/接口测试验证。

## 验收边界

- Python 使用 AST 关系索引；非 Python 语言当前使用保守词法索引并明确标记降级，不把词法结果宣称为完整语义分析。
- 真值评测降低模型波动带来的发布风险，但不能把概率模型变成形式化验证器。
- FlyTrap 历史数据、程序目录、unit 文件、证书和队列均未删除，满足审计和回滚需要。
- 磁盘风险已通过受控清理从 89% 降至 84%，最终运维巡检不再降级；但仅低于 85% 告警线 1 个百分点，仍需保留容量监控。
- 额外隔离恢复暴露了生产 MySQL 900 MiB 内存上限不足以安全承载全量临时恢复；自动重启后崩溃恢复、关键表、迁移、日志和公网验收均通过，验证架构改造列入非阻断 TODO。

## 后续生产复核补充（2026-09-01 19:51 UTC）

本验收记录中的 3.8.1、`c8a80228`、84% 磁盘和 `status=ok` 均为当次发布快照。当前生产已运行 3.8.3（`4b1711adf0e79a95eed35be3ab17605fe10869cf`），`/healthz`/`/readyz` 和核心容器均健康；本轮受控清理成功但新增回收为 0B，根分区回到 85% 告警线，巡检仍 `can_continue=true` 且无阻断检查。新建的隔离验证库已清理，历史 `prism_verify_20260819111518_18935` 继续按授权保留。
