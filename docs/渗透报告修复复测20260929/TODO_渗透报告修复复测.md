# 待办：渗透报告修复复测

- [x] 完成候选全量后端测试（5380 通过/5 跳过）、Frontend Vitest（1402 通过）、ESLint/Ruff/build 和 shell/部署脚本检查；E2E 为 51 通过/9 跳过，跳过项为仅限隔离环境的登录型测试。
- [ ] 由独立 Agent 重读验收表、测试输出、v4.0.30 代码和当前生产状态，交叉核验数字与结论。
- [ ] 提交 ReviewTaskList 首载空态修复、VERSION 4.0.30 和本次验收更新；生成精确 SHA bundle，排除未跟踪 `backend/.venv311/`。
- [x] 发布 v4.0.29：发布备份 gzip/SHA 和 103 表隔离恢复通过；Backend、Frontend、HTTPS/health 健康；Alembic 保持 `058_roundtable_sessions`；发布后只读 ops-check `status=ok`。
- [ ] 发布 v4.0.30，并在普通 Safari 同一 `/reviews` 场景确认加载期间不显示“还没有审查任务/0 条”，加载结束后显示 50 条；不得产生审查任务。
- [ ] 在普通 Safari 完成 reviewer/admin 只读角色验收；重复无副作用 SEC-01/02/05 API 测试并记录响应；不创建或删除生产业务数据。
- [ ] 将新证据更新到 ACCEPTANCE/FINAL/TODO，由独立 Agent 复核后运行知识 Harness validate/postflight。

## 明确保留的范围外事项

- 不更改 8888 `BT-Panel` 监听或防火墙。
- 不删除报告中已存在的生产委托 `#7/#8`。
- 未确认的每日任务/Token 预算策略不自行制定；本次只加 HTTP 频率闸门。
- 外部重定向 SSRF、CSP 强制策略等“后续建议”未因本轮发布声明已覆盖。
- 历史 33 个相同 `session_id` 的 owner 归属与完整会话泄漏风险仍未由单条 UI 搜索排除；不得写为全局账号隔离通过。
- 生产自动回滚首次因 release 缺 Certbot 外部 bind mount 未能恢复；当前 v4.0.29 经候选目录链接到原生产证书后健康。v4.0.30 发布沿用已验证的 dotenv 与证书目录链接，不能移除。
