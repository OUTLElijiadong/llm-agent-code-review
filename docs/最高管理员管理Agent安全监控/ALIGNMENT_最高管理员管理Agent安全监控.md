# ALIGNMENT：最高管理员管理 Agent（安全监控与主动告警）

> 创建日期：2026-08-05
> 状态：等待用户确认关键决策点（见文末"疑问澄清"）

## 一、原始需求（用户原话要点）

1. **不给服务器做安全配置**——不把"服务器加固"作为交付物。
2. 由**项目里的最高管理员管理 Agent** 负责：
   - 监控**每一次登录**（SSH/应用）和**疑似网络攻击**；
   - **反向探查**攻击来源与攻击手法；
   - 输出**服务器优化建议**；
   - 监控**对生产数据产生威胁**的所有网络攻击；
   - **定期备份**并**定期删除旧备份**（释放空间，便于数据恢复）；
   - 攻击发生时**主动汇报**：管理员在线 → 右上角弹窗；不在线 → 等上线后自动弹窗；
   - 告警类别：网络安全、被渗透、生产库破坏、数据泄漏、服务器运维问题等，并提供**解决建议**；
   - 配置对应的 Agent 能力（参考 ZOZO SOC Agent、IETF AI 安全智能体、Agentic SOC、ATT&CK 协同渗透、告警富化等资料）。
3. 最终目标语句："请你帮我修复我的生产服务器" → 已澄清为**由 Agent 上线后自动发现问题、汇报并给出解决建议**，而非由 Codex 直接 SSH 修复。

## 二、项目上下文（已有资产，全部复用，不新建重复组件）

| 资产 | 位置 | 现状 |
| --- | --- | --- |
| `operations` 全服管理 Agent | `backend/app/agents/operations_agent.py` | 已有，职责为整机巡检/受批准变更；展示名"全服管理 Agent" |
| Root 结构化执行器 | 生产 `/opt/prism-releases/e693d2117.../deploy/prism_ops_executor.py` | 已具备 host_inventory/list_directory/read_text_file/journal_query/systemd/docker/write_text_file/package/firewall/account/ssh_authorized_key 动作；只读动作自动执行，写动作需审批 |
| 唯一超级管理员 | `docs/唯一超级管理员与单设备登录/` | `admin` 用户 + `super_admin` 角色 + `server_ops:*` 权限；普通管理员不可见服务器工具 |
| 管理副驾驶 | `backend/app/agents/admin_copilot_agent.py` + `admin_copilot_service.py` | Responses 规划 + 持久化审批 + 幂等执行账本 |
| 平台通知面 | 前端路由 `admin/observability`（监控告警页 = ObservabilityCenter → GovernanceWorkstation） | 尚无站内通知中心/右上角弹窗推送能力（grep 未发现 notification API） |
| 备份 | `deploy/backup.sh` / `verify-backup.sh` / `restore.sh` + systemd timers | 每日 02:15 备份、每周日隔离恢复验证、默认保留 14 天自动删旧（`find -mtime +14 -delete`） |
| 巡检 | `deploy/ops-check.sh` + prism-ops-check.timer | 每 5 分钟 JSON 巡检，当前全绿 |
| 蜜罐 | `/opt/flytrap/agent`（systemd `flytrap-agent`） | 在 3306/6379/2222/2323/8080 伪装服务捕获攻击，日志实时记录攻击者 IP/用户名；控制端 42.194.238.178:8443（腾讯云） |
| 网关日志 | `cr_frontend` Nginx 容器日志 | 可提取 400/403/CONNECT 代理探测/扫描指纹 |
| 应用登录 | 平台登录/会话体系 | `token_version` 单会话世代号；登录事件可入审计 |

## 三、服务器现状快照（2026-08-05 只读侦察，作为 Agent 初始基线与首个待办样本）

> 用户已指示停止由 Codex 直接侦察；以下为已完成侦察的结论，供 Agent 上线后复核与接管。

- 生产拓扑：`cr_backend / cr_mysql / cr_frontend / cr_clamav / cr_redis` 五容器全 healthy；`ops-check` exit=0；Alembic `026=head`；HTTPS 正常；最近备份 23h 前且 gzip/SHA 校验通过。
- 系统：OpenCloudOS 2 核 / 7.5GiB；磁盘 52G/80G（65%）；Swap 已用约 995MiB；负载 0.7~1.0。
- SSH：端口 22；`PermitRootLogin without-password`；`PasswordAuthentication no`；`MaxAuthTries 3`（已较安全，Agent 不应改动）。
- 登录基线（近 14 天 Accepted）：主要来自 117.141.246.34 / 117.141.144.97 / 117.141.145.246 / 39.144.135.*（用户家宽）；**另有 3 把密钥在途**：
  1. `SHA256:iFmj61r60Z0iNNxh+Nc6qx4kYPDpwzNLvB6FV1ikoN4`（RSA，= gpt.pem，主用，3868 次）；
  2. `SHA256:wbLkqbw/WvhqS4M84/JZO2Lm+LdU59ovc+N70q/SVf4`（ED25519，1189 次；6/12 起从 `217.28.137.70` 使用，8/4 从 `45.135.228.155` 使用；**本地密钥目录无此私钥**）；
  3. `SHA256:QMGEeLXiu6IGGJj6thdv29zLHkWVUs0tUiyh2+ZAwDw`（87 次，来源待 Agent 复核）。
- 攻击面现状：FlyTrap 蜜罐 24h 捕获 3 万+ 次 SSH 密码爆破（来源 TOP：138.197.180.155、103.195.81.146、193.32.162.18/25/30、195.178.110.4 等）；Nginx 存在 CONNECT 代理探测（104.249.59.148、45.135.193.193、204.76.203.x 等）与 TLS 探测（172.236.228.227）。
- 备份目录 `/opt/code-review/backups` 已占 **7.4GB**（67 个 .sql.gz，仅 2 个超 14 天；其余为手工/发布产物 tar.gz、快照目录，不受 backup.sh 保留策略管理）。
- 其他服务（只读盘点，Agent 不处置）：FlyTrap、`/opt/lijiadong-portfolio`(18787)、`/opt/momentum-radar`(19190)、`/opt/auto-surface-mm-local`(8621)、宝塔面板(8888)、Postfix(25)。
- 待用户认领：`wbLkqbw...` 与 `QMGEeLXiu...` 两把 ED25519 密钥是否属于用户自己的其他机器/CI。

## 四、边界确认（本任务范围）

- **不做**服务器安全配置修改（SSH/防火墙/面板/端口等一律不动，Agent 也不自动改）。
- **不做**主动反打/扫描攻击者主机（违法）；"反向探查"= 被动溯源：日志关联、IP WHOIS/ASN/归属/信誉、ATT&CK 手法指纹、蜜罐与 Nginx/SSH 日志交叉验证。
- **不新建**职责重复的第二套运维 Agent：能力并入现有 `operations`（展示名升级为"最高管理员管理 Agent"）或经用户确认后独立命名。
- 只读动作自动执行；写动作（清理备份等）走现有 Responses 审批门禁（critical 需"确认执行"）。
- 非本项目服务（FlyTrap、宝塔、作品集、行情雷达等）只读监控，不处置。

## 五、需求理解（初步方案骨架，待共识确认）

1. **监控采集层（只读）**：在 Root 执行器新增只读动作——`ssh_login_events`（journalctl sshd 成功/失败登录）、`flytrap_attack_events`（蜜罐捕获）、`nginx_attack_events`（Nginx 日志指纹）、`backup_audit`（备份清单/年龄/占用）。Agent 定时（可复用 scheduler/timer）拉取。
2. **检测与富化层**：规则引擎（阈值聚合：失败次数/来源/端口）→ 攻击类型分类（暴力破解/代理滥用/TLS 探测/端口扫描/蜜罐触碰）→ ATT&CK 技术映射 → IP 被动溯源（WHOIS/ASN/国家/信誉，内置或调用公共接口）。
3. **Agent 层**：升级 `operations` 系统提示与技能，加入"安全监控、攻击溯源、备份治理、优化建议"；周期性自动巡检；对每次成功 SSH 登录（非白名单 IP）与高危攻击生成告警。
4. **告警通知层**：后端新增站内通知表 + SSE/WebSocket 推送 + 未读持久化；前端右上角 `ElNotification` 弹窗（在线即弹；离线则登录后拉取未读自动弹出）+ 管理页通知中心/安全监控页。
5. **备份治理**：Agent 周期核对备份新鲜度/校验/占用；触发 `backup_database`、`verify_backup`；对超期备份发起审批清理（含手工产物清理阈值）。
6. **优化建议**：Agent 依据巡检数据（磁盘/内存/Swap/容器镜像堆积/备份体积）输出可执行建议（如清理旧镜像、释放 Swap、备份异地化），经审批后执行。

## 六、疑问澄清（等待用户回答，按优先级）

- Q1 Agent 形态：扩展现有 `operations`（推荐）还是新建独立 Agent？
- Q2 反向探查边界：确认"只被动溯源、不主动反打"？
- Q3 采集方式：允许通过现有执行器**新增只读动作**读取 SSH/蜜罐/Nginx 日志（不改任何配置）吗？
- Q4 告警弹窗：右上角弹窗 + 站内通知中心 + 离线持久化登录后自动弹（推荐），还是仅站内通知？
- Q5 备份保留：沿用 14 天（推荐）？手工产物清理阈值（如保留 30 天）？
- Q6 交付范围：本轮仅开发+本地测试，还是包含生产部署（需走发布流程）？
- Q7 密钥认领：`wbLkqbw...` / `QMGEeLXiu...` 是否你的其他机器/CI 在用？
