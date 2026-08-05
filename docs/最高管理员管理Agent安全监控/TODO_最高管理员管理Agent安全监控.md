# TODO：最高管理员管理 Agent 安全监控与主动告警（待办清单）

> 更新日期：2026-08-05（全任务完成后修订）
> 代码已提交：`a87ba07`（分支 `codex/admin-security-monitor`）

## 一、需要你确认/决策

1. **SSH 白名单 CIDR 尚未配置**：`SECURITY_SSH_ALLOWLIST_CIDRS=[]` 默认为空。
   → 请在 `deploy/.env`（生产）配置你的家宽/办公出口网段（如 `["117.141.246.0/24","39.144.135.0/24"]`），否则所有非白名单成功登录都会 high 弹窗。
2. **两把 ED25519 密钥归属待认领**（ALIGNMENT Q7）：
   - `SHA256:wbLkqbw/WvhqS4M84/JZO2Lm+LdU59ovc+N70q/SVf4`
   - `SHA256:QMGEeLXiu6IGGJj6thdv29zLHkWVUs0tUiyh2+ZAwDw`
   → 若不属于你本人/CI，请走 SSH 授权清理流程（写操作需唯一超级管理员审批）。**若你确认不是自己的，这是 Agent 上线后要立即处理的最高优先级事件（疑似密钥泄露/入侵）。**
3. **安全监控调度间隔**：默认 `interval@5m`（任务字面值）。如想改用 `SECURITY_MONITOR_INTERVAL_MINUTES` 配置控制，可在管理端"调度任务"页修改 security_monitor 的 schedule，或后续把 `_DEFAULT_JOBS` 改为读取配置（当前配置项已注册但未被默认任务引用）。
4. **生产部署时机**：等待干净 commit + CI 全绿后，按 `deploy/RELEASE_CHECKLIST.md §8` 执行（同步执行器 → alembic 027 → 重建 Backend/Frontend → 验收弹窗闭环）。

## 二、生产部署前必须做的

1. **跑迁移**：`alembic upgrade head`（027：agent_alert 加 5 列 + 2 索引）。
2. **同步执行器与 .env**：生产 `deploy/prism_ops_executor.py` 需包含 5 个只读安全动作；`.env` 配置 `SECURITY_*` 与 `THREAT_INTEL_BASE_URL`（可选覆盖，默认 http://ip-api.com/json）。
3. **确认唯一超级管理员存在**：弹窗目标 `User.username=="admin" 且 role=="super_admin"`；不存在则 SSE 弹窗不推送（告警仍入库）。
4. **配置 SSH 白名单**（见上）；确认 45.135.228.155/217.28.137.70 等来源是否本人。
5. **重建并重启 Backend/Frontend**；验证 `POST /api/admin/observability/security/run-monitor` 与前端弹窗。

## 三、运维操作指引

- 手动巡检：`POST /api/admin/observability/security/run-monitor`（唯一超级管理员）。
- 查看态势：`GET /api/admin/observability/security/status?since_hours=24`。
- 未读弹窗：`GET /api/admin/observability/alerts/unread`；已读：`POST /api/admin/observability/alerts/{id}/read`。
- 清理备份/磁盘等写操作：必须在管理 Agent 会话中审批（critical 需输入"确认执行"）后执行。
- 与最高管理员管理 Agent 对话可查询：登录/攻击/备份/优化建议（通过 admin_execute_capability 固定能力）。

## 四、工作区风险提示（重要）

- 开发期间主工作区被外部执行 `git reset --hard`（reflog HEAD@{0}），短暂清空未提交改动后自动恢复。**建议**：
  1. 尽快把 `codex/admin-security-monitor` 推送到远端备份；
  2. 确认没有其他 Codex/Claude 会话在 main 工作区执行破坏性 git 命令；
  3. 后续并行会话改用各自 worktree/分支，避免互相覆盖。
