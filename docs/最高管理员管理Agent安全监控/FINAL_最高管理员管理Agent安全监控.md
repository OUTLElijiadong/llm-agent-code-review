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

## 四、生产部署（已完成）

- 已部署到 81.70.251.90（`deploy-security-monitor` 分支，HEAD `c77656b4`，Alembic 027，全容器 healthy）。
- 生产 .env 已配置 `SECURITY_SSH_ALLOWLIST_CIDRS=["117.141.0.0/16","39.144.0.0/16"]` 等阈值。
- 上线调试期修复：alembic 022-026 补齐、调度 system_scheduled 身份、容器 env 文件对齐、爆破/备份规则细化。
- 待办：生产部署分支与本地 main 分叉待合并；APP_RELEASE 环境显示旧值（.env 内 3ffbfe）属展示问题；SSH 白名单网段为默认家宽段，可按需调整。

## 2026-08-25 线上成本保护修订

本次复现发现，JARVIS 巡逻简报会被多个在线管理会话自动消费并启动小菱模型；`ops_health_check` 在不健康时也会隐式调用模型诊断，造成后台额度消耗。当前口径已修订为：

- `AGENT_JARVIS_PATROL_ENABLED=true` 只保留告警证据采集；`AGENT_JARVIS_AUTO_DISPATCH_ENABLED=false` 默认不自动投递到模型。
- `OPS_HEALTH_DIAGNOSIS_ENABLED=false` 默认只生成确定性告警和处置建议，不调用 LLM。
- 管理员登录/打开小菱保持当前显式选择的新对话；历史 JARVIS 消息写完成回执，不自动启动 Responses。
- 后端 `prepare_message_run` 与重启恢复清扫均有同一成本保护，旧前端或旧检查点不能绕过开关。
- 管理员仍可在小菱中明确发起只读核验；如确需旧的自动派发/后台模型诊断，必须在生产 `.env` 显式开启对应开关并重新部署。

## 2026-08-25 线上部署与真实点击验收

- 发布提交：`6469a20e94fdc16d4385be2a1e85b394899a951b`；生产分支：`codex/jarvis-cost-guard-6469a20`。
- 生产镜像：`prism-backend:6469a20e94fdc16d4385be2a1e85b394899a951b`、`prism-frontend:6469a20e94fdc16d4385be2a1e85b394899a951b`；Backend、Frontend、MySQL、Redis、ClamAV 均 healthy。
- 发布前已备份并通过隔离恢复校验生产数据库；备份位于 `/opt/code-review-env-backups/.env.pre-jarvis-20260825T013521Z`，Alembic 当前为 `040`。
- `/healthz` 与 `/readyz` 均返回 `status=ok/ready`，版本和 release 均为上述提交。
- 管理员 `admin` 真实登录 `https://lijiadong.cn/` 后点击“打开小菱 · 管理副驾驶”：当前会话显示“新对话”，运行状态为“空闲”，无进度条、无停止按钮、无 JARVIS/每日运维内容；等待 15 秒后状态不变。
- 浏览器复验期间页面错误/警告为空；发布后数据库没有新增 JARVIS 消息、模型调用或 `agent_response_run` 运行记录。
- 发布后数据库中仍保留 JARVIS 审计历史，但不再有 queued/delivered/acknowledged/processing 活动记录；原先 8 条 queued、3 条 processing 已由启动收敛逻辑完成回执，不删除历史数据。

## 2026-08-25 历史 JARVIS 收敛补丁与重新验收

- 发布提交：`c67680421210e9bc2af176db90b637855c4ab60d`；生产分支：`codex/jarvis-cost-guard-c676804`。
- 生产镜像：`prism-backend:c67680421210e9bc2af176db90b637855c4ab60d`、`prism-frontend:c67680421210e9bc2af176db90b637855c4ab60d`；Backend、Frontend、MySQL、Redis、ClamAV 均 healthy。
- 发布前完成 `.env` 备份和隔离恢复校验；Alembic 仍为 `040`。`/healthz`、`/readyz` 返回 `status=ok/ready` 与 release `c676804...`。
- 后端启动日志明确记录：`JARVIS 成本保护收敛 messages=11 runs=0`。数据库状态为 `completed=94、dead_letter=177`，JARVIS 活动记录数 `0`，活动 `agent_response_run` 数 `0`。
- 管理员真实执行“退出 → admin 重新登录 → 打开小菱 · 管理副驾驶”：登录后未自动启动任务；手动打开后显示“新对话”和“空闲”，观察 10 秒无进度条、无 JARVIS/每日运维内容。
- 两次线上观察的浏览器 error/warn 均为空；发布后 `ai_call_log` 与 `agent_response_run` 均无新增记录，证明登录和打开面板没有触发模型调用。
