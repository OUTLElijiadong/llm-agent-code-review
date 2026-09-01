# Todo：多智能体可信审查与 FlyTrap 退役

## 阻断待办

无。当前没有缺失配置、未执行迁移、未关闭的 FlyTrap 生产告警或待人工决定的发布事项。

## 容量治理（已完成，持续监控）

- 早前 `cleanup.sh --apply` 已释放 `9.29 GiB`；本次发布后再次按同一保护策略执行成功，新增回收为 `0B`，根盘当前 85%（约 29 GB 可用），属于非阻断告警。
- 当前与上一版镜像、运行中镜像、数据库卷、证书、备份和发布账本全部保留；`ops-check` 返回 `can_continue=true`、`blocking_checks=[]`。
- 清理审计位于 `/opt/code-review/backups/controlled-cleanup-20260901T145933Z`，所有文件哈希已复验。
- 阈值内的近期构建缓存依照保留策略未删除，不需要进一步破坏性清理。
- 当前 85% 已触及告警线；继续使用现有自动巡检监控，保持 `can_continue=true`。若继续增长，再重新评估保留策略或扩容。

## 备份验证资源隔离（已完成）

- `deploy/verify-backup.sh` 已将恢复目标固定为专用 `cr_testdb`，并拒绝 `cr_mysql` 或同容器目标；资源门禁不足时安全退出。
- 最新发布备份 `code_review_20260901T192255Z_4b1711adf0e7.sql.gz` 已在 `cr_testdb` 完成隔离恢复，识别 89 张表、Alembic `046_finding_aggregation`；本次新建校验库已清理。
- 历史 MySQL 900 MiB 容器内的 OOM 事件已作为历史证据保留，未再重复触发；历史临时库 `prism_verify_20260819111518_18935` 未经授权保持未动。
- 当前没有备份验证阻断；异地备份副本和容量扩容属于独立的长期运维能力，不在本次应用发布范围内。

## 持续改进

以下项目不影响本次验收，不是遗留故障：

1. 持续扩充 NIST SAMATE/SARD 风格真值集，重点增加跨文件、动态分派、装饰器、反射和 sanitizer 语义样本。
2. 当业务需要更强的 JavaScript/TypeScript/Java/Go/C++ 跨函数分析时，再按独立变更引入相应 AST/tree-sitter 解析器；当前词法降级状态已明确可见。
3. 按模型版本、提示词版本和分片器版本持续观测精确率、召回率、路径召回和不稳定率；指标恶化时由现有评测门禁阻止自动推广。

## 配置约束

- 保持后端 `SECURITY_FLYTRAP_ENABLED=false` 和运维执行器 `PRISM_FLYTRAP_ENABLED=false`；只有 FlyTrap 项目正式恢复并完成新生产验收后才能重新开启。
- 不删除 `/opt/code-review/backups/flytrap-retirement-20260901T111647Z`，它是退役审计和恢复依据。
- 不把人工驳回当作模型永久负样本直接全局生效；进入提示词或策略前继续走现有评测与审批链。

## 需用户决定

无立即阻断决策。磁盘容量治理已完成；备份验证资源隔离应作为后续独立开发变更。
