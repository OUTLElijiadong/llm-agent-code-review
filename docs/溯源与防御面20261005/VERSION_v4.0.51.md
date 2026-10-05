# v4.0.51 溯源与防御面（版本说明）

> 代码基线：生产 v4.0.50 / `2ac870e02e0add7972aeef2a01b36c04a6b06b07`（分支 `codex/security-center-ux`）
> 本版目标：把"被打了以后只看到一条告警"补成**可核验的溯源与防御面证据链**；
> 处置手段仍然是既有的**限时单 IP 封禁租约**，不引入任何新形式的对外动作。

## 1. 一句话差异

v4.0.50 的安全中心能告诉你"有人在打"，不能告诉你"这个人是谁、打过什么、我这边还漏着什么"。
v4.0.51 在安全中心新增第四个分区「溯源与响应」，用三个**只读**宿主机动作把这条链补齐：

| 能力 | 宿主机动作 | 读什么 | 输出什么 |
| --- | --- | --- | --- |
| 单来源溯源 | `security_ip_trace` | `journalctl -u sshd`、`docker logs cr_frontend`、租约账本、被动情报源 | SSH 失败次数与尝试账号、敏感目标探测次数与路径、处置记录、被动归因、确定性风险分 |
| 防御面审计 | `security_surface_audit` | `ss -lntup`、`iptables/ip6tables -S INPUT|DOCKER-USER`、`command -v` | 监听面、对外监听、防火墙链可读性、加固应用已装/未装、ipset 与租约状态 |
| 流量元数据 | `security_traffic_summary` | `ss -H -tunap` + 两类可信日志计数 | 对端 IP、端口、协议、状态、进程、攻击计数；**不含载荷** |

## 2. 明确的能力边界（不夸大）

- **不抓包**：`payload_captured` 是代码里的固定 `false`。没有 tcpdump、没有 MITM、没有载荷落盘。
- **不主动出击**：溯源只读本机日志与内核状态；不向被溯源地址发包、不连端口、不扫描。
- **没有"反击"这个开关**：安全中心里的 `counterattack_enabled` 仍恒为 `false`；本版只把
  它旁边新增 `trace_capability_available`，如实反映"只读取证是否可用"，不再让页面把只读能力
  渲染成一个从不开启的开关。
- **唯一处置动作 = 既有限时封禁**：确定性规则（SSH 20 次 / Web 30 次且 ≥3 个敏感目标）
  或小菱脱敏候选研判（SSH ≥10 / Web ≥10，最长 120 秒），内核 ipset + TTL 自动到期；
  保护来源（当前管理员连接、服务器地址、近一小时 SSH 成功来源、白名单、非公网）永不处置。
- **模型不能自封 IP**：三个新动作全部登记为 `INTERNAL_SECURITY_ACTIONS`，不进运维工具目录，
  也不能下发给子 Agent 团队或 Agent Mesh（为此新增 `TEAM_READ_ONLY_ACTIONS`，把内部安全动作
  从团队只读清单里剔除）。

## 3. 风险评分口径

`security_ip_trace` 的分值只吃**本机可信日志证据**：

| 证据 | 加分 |
| --- | --- |
| 窗口内存在 SSH 密码失败 | +3 |
| 窗口内存在敏感目标探测 | +3 |
| 同一来源同时命中两类证据 | +2 |
| 覆盖 ≥3 个不同敏感目标 | +1 |
| 该来源属于受保护地址 | +2 |
| 当前存在生效中的封禁租约 | +1 |

`score = min(100, 加分 × 10)`，`level`：≥70 critical / ≥50 high / ≥30 medium / 其他 low。
出网归因（国家、地区、城市、ISP、Org、AS）单独分栏展示，**不参与计分**，且失败不影响本机结论——
生产实测里 `ip-api.com` 出现过超时，此时仍返回完整本机证据。

## 4. 数据来源与脱敏

- SSH 证据：`journalctl -u sshd --output=json`，用 `__CURSOR` 区分同一微秒事件；
- Web 证据：`docker logs cr_frontend`，只认直连 `remote_addr` 与固定敏感路径清单，**不信任 XFF**；
- 只输出计数、账号名、路径、状态码分布与时间范围，不输出请求正文、口令、Cookie；
- 后端 `security_trace_service` 对执行器回执做白名单归一化：归因只保留
  `country/region/city/isp/org/as`，未知字段丢弃（已测：注入 `secret_token` 不回流）。

## 5. 改动清单

**宿主机执行器**

- `deploy/prism_security_block.py`：新增 `parse_ssh_attacker_summary`、`parse_web_attacker_summary`、
  `_defense_record`、`_optional_command`、`_outbound_attribution`、`ip_trace`、`surface_audit`、
  `traffic_summary`；`execute()` 新增三个动作分派并拒绝多余参数。同时修正 Web 解析正则
  在带引号 UA 日志行上的匹配缺陷（原 `[^ ]+` 会吞掉 `[` 前的令牌导致整行不匹配）。
- `deploy/prism_ops_executor.py`：三个动作进 `READ_ONLY_ACTIONS` 与参数白名单；
  新增 `SECURITY_MODULE_ACTIONS`，与 `security_block_*` 共用同模块分派。

**后端**

- `backend/app/services/security_trace_service.py`（新增）：回执校验 + 字段白名单归一化，
  无有效回执时返回 `available=False`，不用默认值冒充证据。
- `backend/app/services/ops_service.py`：三个动作登记风险等级、内部安全动作集合、
  只读集合、参数键/类型，以及参数校验（溯源只收单 IP、流量窗口 1–72 小时）；
  新增 `TEAM_READ_ONLY_ACTIONS`。
- `backend/app/api/v1/admin_security_center.py`：`POST /admin/security-center/trace/ip`、
  `GET /admin/security-center/surface`、`GET /admin/security-center/traffic`，全部 `require_super_admin`。
- `backend/app/schemas/security_center.py`：`SecurityTraceIn`（拒绝 CIDR 与多值）。
- `backend/app/services/security_center_service.py`：策略回执新增
  `trace_capability_available` / `trace_capability_label`。
- `backend/app/services/agent_mesh_dispatcher.py`、`agent_team_service.py`、`agent_mesh_service.py`：
  团队与网格只读清单改用 `TEAM_READ_ONLY_ACTIONS`。

**前端**

- `frontend/src/api/adminSecurityCenter.ts`：三个新接口与类型。
- `frontend/src/views/admin/AdminSecurityCenter.vue`：新增「溯源与响应」分区（溯源卡、防御面卡、
  流量元数据卡），失败/不可用只出错误条与重试，不渲染任何默认数值。
- `frontend/src/components/security/AutomaticBlockingPanel.test.ts`：夹具时间改为相对当前生成，
  修掉一处必然到期失败的时间炸弹（与本次功能无关，但会让完整前端回归红）。

**版本**

- 根目录 `VERSION`：`4.0.50` → `4.0.51`（`deploy/deploy.sh` 以它作为 `APP_VERSION` 唯一来源，
  并校验 `x.y.z` 语义版本）。

## 6. 测试与核验

| 范围 | 命令 | 结果 |
| --- | --- | --- |
| 执行器溯源边界 | `backend/.venv311/bin/python -m pytest deploy/tests/test_prism_security_trace.py -q` | 10 passed |
| 执行器完整套件 | `... -m pytest deploy/tests -q` | 117 passed, 2 skipped |
| 后端溯源服务 | `... -m pytest backend/tests/unit/services/test_security_trace_service.py -q --no-cov` | 11 passed |
| 权限矩阵（含 3 条新路由） | `... -m pytest tests/test_complete_permission_matrix.py -q --no-cov` | 通过（路由基线 346→349、认证路由 332→335、守卫路由 265→268 逐项复核后更新） |
| 安全/运维回归 | `... -m pytest tests/unit/services/test_server_ops_service.py tests/unit/services/test_security_response_service.py tests/unit/api/test_security_monitor_api.py -q --no-cov` | 见证据文件 |
| 前端专项 | `npx vitest run src/views/admin/AdminSecurityCenter.test.ts src/components/security/AutomaticBlockingPanel.test.ts` | 52 passed |
| 前端完整 | `npx vitest run` | 见证据文件 |
| 前端检查与构建 | `npx eslint ... --max-warnings=0`；`npx vue-tsc --noEmit`；`npm run build` | 0 problems / 0 诊断 / 构建成功 |

**生产宿主只读实测**（未发布，方法与本轮"未做"清单见
[生产宿主只读实测.txt](证据/生产宿主只读实测.txt)）：

- 监听面：对外仅 22 / 80 / 443；MySQL 只监听 `127.0.0.1:3307`；3306/6379/8888 外部 closed。
- 防火墙：IPv4/IPv6 的 `INPUT` 与 `DOCKER-USER` 均可读，`PRISM-SEC-IN` / `PRISM-SEC-DK`
  与实际业务链共存。
- 加固应用：**仅 ipset 已安装**；fail2ban / nmap / suricata / zeek / whois 均未安装（未擅自安装）。
- 溯源实测：管理员来源被正确识别为**受保护来源**；非公网地址不发外部请求；
  外部情报源超时时仍返回完整本机证据。
- 封禁链路自检：对回环地址 `127.0.0.9` 下发 120 秒内核租约，`ipset save` 显示 TTL 生效、
  两条链引用正确、`ipset del` 后集合清空；随后把 `/var/lib/prism-ops/security-block`
  从备份完整还原（entries=0、enabled=True），临时模块已 `shred` 删除。

## 7. 发布步骤（未执行）

```bash
# 1) 在 worktree 打包并校验
cd "/Users/li/.codex/worktrees/v4-1m-context-retest/基于大模型智能体的代码审查平台"
git add -A && git commit -m "feat: add read-only attacker tracing and defense surface audit for v4.0.51"
git bundle create /tmp/prism-4.0.51.bundle codex/security-center-ux

# 2) 在服务器按既有完整版本事务发布（备份 → 恢复校验 → 镜像 → 迁移 → 健康核验）
scp /tmp/prism-4.0.51.bundle root@81.70.251.90:/tmp/
ssh root@81.70.251.90 'cd /opt/code-review && deploy/deploy.sh /tmp/prism-4.0.51.bundle'

# 3) 发布后核验：healthz/readyz 版本、执行器目录、安全中心四个分区真实点击
curl -sS https://lijiadong.cn/healthz
```

## 8. 下一步建议（需管理员显式授权）

1. 安装 `fail2ban`（仅 sshd jail）+ `whois`：前者把登录爆破处置从"每小时日志巡检"提前到秒级，
   后者补齐溯源里的归属摘要；两者都不改变现有 ipset 链路。
2. 可选装 `nmap` 仅用于**本机**端口自查（`nmap -sT 127.0.0.1`），不要对第三方地址使用。
3. `suricata` / `zeek` 需要镜像流量与额外内存，建议在业务低峰单独评估后再上。
4. 生产 `.env` 可显式配置 `THREAT_INTEL_BASE_URL`，避免默认端点在某些出口被限速或阻断。
