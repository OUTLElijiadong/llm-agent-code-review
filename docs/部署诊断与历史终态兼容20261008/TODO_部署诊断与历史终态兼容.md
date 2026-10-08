# 待办：部署诊断与历史终态兼容

- [ ] 提交精确 v4.0.86 候选并推送；等待 Business CI 与 CodeQL 成功。
- [ ] 生成并校验包含完整历史的 Git bundle；生产端复验字节数和 SHA-256。
- [ ] 发布前检查精确备份与隔离恢复、磁盘空间、发布账本/当前和上一镜像；只使用 `deploy.sh all --revision <FULL_SHA>`。
- [ ] 完成部署后核对运行镜像、SHA、`APP_RELEASE`、Alembic、health/ready、HTTPS 与前端 buildInfo/资产。
- [ ] 普通 Safari 刷新历史黑盒任务，确认模式与终态正文一致；确认审计原始事件未被改写。
- [ ] 记录 v4.0.86 发布和观察窗口结果；失败时保留脚本阶段、退出码与回滚回执。
- [ ] 继续完整黑白盒业务验收（可运行真实 Web 项目、无入口负例、loopback 正例及重复样本）；此项不由历史文案修复替代。
- [ ] 由负责人审阅生产 cleanup dry-run；本轮不执行清理或系统 prune。

