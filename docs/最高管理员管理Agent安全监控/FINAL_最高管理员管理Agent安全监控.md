# FINAL：最高管理员管理 Agent 安全监控与主动告警（交付总结）

> 交付日期：2026-08-05；提交 `a87ba07`（分支 `codex/admin-security-monitor`）

## 一、交付内容

### 1. 采集层（deploy/prism_ops_executor.py，只读，不改服务器配置）
- `ssh_login_events`（journalctl sshd：成功/失败/无效用户，按 IP/密钥指纹聚合）
- `flytrap_attack_events`（蜜罐 JSON 日志解析）
- `nginx_attack_events`（CONNECT 代理探测 / TLS 乱码 / 400-444 指纹）
- `backup_audit`（备份清单/年龄/校验/体积/手工产物）
- `ip_attribution`（固定 URL 被动溯源，ipaddress 校验，15s 超时）
- 解析纯函数 parse_ssh_log / parse_flytrap_log / parse_nginx_log 可单测。

### 2. 契约层（backend/app/services/ops_service.py）
- 5 动作注册 risk=low/read_only；`SCHEDULER_READ_ACTIONS` 允许 source="scheduler" 无交互只读调用；交互仍要求唯一超级管理员。

### 3. 检测层（backend/app/services/security_monitor_service.py）
- 规则引擎 + 指纹去重 + 高危 IP 被动溯源富化 + SSE admin_alert 弹窗。
- `run_security_monitor` / `query_security_status`；阈值全部配置化。

### 4. 数据模型（Alembic 027）
- `agent_alert` 新增 category/source/user_id/read_at/fingerprint + ix_agent_alert_user_read、ix_agent_alert_fingerprint。

### 5. 调度
- `security_monitor` interval@5m（仅超级管理员可改）；`SECURITY_MONITOR_ENABLED` 总开关。

### 6. API（/api/admin 前缀，共 4 个）
- `GET /observability/alerts/unread`（require_admin）
- `POST /observability/alerts/{id}/read`（require_admin，归属校验）
- `POST /observability/security/run-monitor`（require_super_admin）
- `GET /observability/security/status`（require_super_admin）
- `admin_capability_registry` 新增 4 条能力（observability.security.*）。

### 7. Agent 升级
- operations → "最高管理员管理 Agent"：description/skills/system_prompt/契约（含 security_monitor Skill）。

### 8. 前端
- `useSecurityAlerts`：全局右上角 ElNotification；未读拉取/已读标记；SSE 实时弹窗；sessionStorage 去重；响应式启动（刷新/登录后 profile 异步加载也生效）；不新增管理页。
- App.vue 挂载；agentEvent 类型增加 admin_alert；AgentChatDrawer/AgentCenter 类型收敛。

### 9. 部署与文档
- RELEASE_CHECKLIST §8 安全监控发布验收步骤；.env.example 增加 SECURITY_* 样例；docs/最高管理员管理Agent安全监控/ 6 份 6A 文档；docs/generated 基线刷新。

## 二、质量门禁
- 后端本功能 22 项新测试 + 相关回归全绿；前端 147 项全绿 + 构建成功；ruff/compileall/契约/部署脚本 PASS。
- 干净 worktree 中 5 failed/6 errors 均为其他并行会话未提交的沙箱测试，与本功能无关。

## 三、风险与建议
- **工作区共享且存在外部 reset**：开发期间发生过一次 `git reset --hard`（短暂清空后恢复），建议尽快把本分支推送到远端并提醒其他会话不要在 main 工作区执行破坏性 git 操作。
- **SSH 白名单为空**：生产上线前必须配置 SECURITY_SSH_ALLOWLIST_CIDRS，否则每次非白名单登录都会 high 弹窗。
- **两把 ED25519 密钥归属未认领**：wbLkqbw.../QMGEeLXiu... 若非本人/CI，需走 SSH 授权清理审批。
