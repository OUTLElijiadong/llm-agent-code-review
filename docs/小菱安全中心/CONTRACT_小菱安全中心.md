# 小菱安全中心 · 接口契约与分工交接

> 版本：v1（backend 主体已落地，待前端与导出/趋势补齐）
> 目标：给管理员小菱（Agent 编码 `admin_copilot`）一套**真正生效**的安全口径，
> 并在管理台里有一个能看、能调、能演练的安全中心。

---

## 0. 分工

| 角色 | 范围 | 文件所有权（不要越界改动） |
| --- | --- | --- |
| Fox（本会话） | 网络安全：规则库、策略引擎、强制点接线、态势评分、策略 API、单测 | `backend/app/services/copilot_security_*.py`、`backend/app/schemas/copilot_security.py`、`backend/app/api/v1/copilot_security.py`、`backend/app/services/tool_gateway.py`、`backend/app/api/v1/agent_responses.py`、`backend/tests/test_copilot_security_*.py` |
| Claude | 前端：安全中心页面 + api/types + 管理台 tab + 前端测试 | `frontend/src/views/admin/CopilotSecurityCenter.vue`、`frontend/src/views/admin/CopilotSecurityCenter.test.ts`、`frontend/src/api/copilotSecurity.ts`、`frontend/src/types/copilotSecurity.ts`、`frontend/src/views/admin/AdminUnifiedCenter.vue`（只加一行 tab） |
| codex | 非安全后端与逻辑：事件导出、趋势聚合、运维文档 | `backend/app/services/copilot_security_export_service.py`、`backend/app/api/v1/copilot_security_analytics.py`、`backend/tests/test_copilot_security_analytics.py`、`docs/小菱安全中心/` |

**互不重叠**：任何人不要重写别人的文件；需要改别人的文件时先在这里记一行说明。

---

## 1. 后端已落地的东西

### 1.1 强制点（不是配置表摆设）

| 位置 | 作用 |
| --- | --- |
| `POST /api/agent-responses/stream` | 进会话前跑 `guard_request`：紧急停机 → IP 白名单 → 会话 IP 绑定 → 空闲/绝对超时 → 消息长度 → 提示注入检测。命中即 403（业务码 `40330`，`detail.reason_code` 区分原因）。 |
| `tool_gateway.execute` / `authorize` | 每次 Agent 工具调用在既有策略引擎之后追加 `tighten_tool_decision`：停机 / 工具黑白名单 / 禁止 shell / 禁止改生产配置与凭据 / 写操作转审批 / 单轮与每小时预算。**只收紧，永不放宽**。 |
| `tool_gateway._write_log` | 工具日志落库前按策略脱敏（密钥、连接串、手机号、内网 IP、身份证、邮箱）。 |
| `audit_log` | 所有拦截与降级写 `copilot_security.<kind>` 事件，详情已脱敏；`alert_on_block` 打开时同时生成治理告警。 |

### 1.2 策略字段（`system_config` 键 `copilot_security`）

完整字段见 `backend/app/schemas/copilot_security.py::CopilotSecurityPolicyOut`（31 个字段）。
默认值见 `backend/app/services/copilot_security_rules.py::DEFAULT_POLICY`：**安全开关全开、
但所有会挡正常使用的闸门（IP 白名单、超时、预算）默认放行**，保证升级当天不改变既有行为。

四档预设（`PRESETS`）：

| 预设 | 口径 |
| --- | --- |
| `monitor` | 只记录不拦截（阻断阈值 101） |
| `balanced`（默认） | 拦明显注入、禁 shell 与生产配置、不设预算 |
| `strict` | 注入阈值 50、绑定 IP、空闲 1h / 绝对 12h、写操作转审批、单轮 40 次 / 每小时 240 次 |
| `max`（最顶级） | 注入阈值 40、绑定 IP、空闲 30min / 绝对 8h、写操作转审批、单轮 24 次 / 每小时 120 次、消息 20000 字符 |

> 切预设**不会**清空管理员手填的 `ip_allowlist` / `tool_allowlist` / `tool_denylist` / `kill_switch`。

### 1.3 API 契约

统一前缀 `/api/admin/copilot-security`，响应包一律 `{code, message, data}`。
权限：读与演练 = 管理员 + `security:view`；写 = 仅唯一超级管理员 `admin`。

| 方法 | 路径 | 请求 | 响应 `data` |
| --- | --- | --- | --- |
| GET | `/center` | — | `{policy, posture, presets[], event_kinds[], governed_agent}` |
| GET | `/policy` | — | `CopilotSecurityPolicyOut` |
| PUT | `/policy` | 局部字段（只提交要改的） | `{policy, changed[], posture}` |
| POST | `/policy/preset` | `{preset, confirm}`（`max` 必须 `confirm=true`） | `{policy, changed[], posture}` |
| POST | `/kill-switch` | `{enabled, reason?}` | `{policy, changed[], posture}` |
| GET | `/posture` | — | `CopilotSecurityPostureOut` |
| POST | `/scan` | `{text}` | `{injection{score,hits[],obfuscation}, redacted, redaction_hits[], original_length}` |
| GET | `/events` | `page, page_size, kind?, status?, days=7` | `{items[], total, page, page_size, pages}` |

错误口径：

- `40330` + `detail.reason_code` ∈ `kill_switch | ip_not_allowed | session_ip_mismatch | session_timeout | message_too_long | prompt_injection`
- `40001` 策略字段非法（如 `ip_allowlist` 里写了 `10.0.0.0/99`）
- `400/422` 提交了未声明字段
- `403` 非管理员 / 二级管理员尝试写

`posture` 结构：`{score 0..100, grade A+..E, checks[{key,title,weight,critical,passed,remediation}],
recommendations[], critical_gaps[], events{window_hours,total,blocked,by_action{}}, policy{...}, generated_at}`
共 22 个检查项，权重求和后折算分值；`max` 预设 + 填好 `ip_allowlist` + `tool_allowlist` 可达 A/A+。

---

## 2. Claude 任务：前端「小菱安全中心」

### 2.1 文件

1. `frontend/src/types/copilotSecurity.ts` —— 与 `backend/app/schemas/copilot_security.py` 一一对应（接口名保留后端 Out/In 命名）。
2. `frontend/src/api/copilotSecurity.ts` —— 用 `@/api/http` 的 `get/put/post` 封装 8 个接口：
   `getCopilotSecurityCenter / getCopilotSecurityPolicy / updateCopilotSecurityPolicy /
   applyCopilotSecurityPreset / toggleCopilotSecurityKillSwitch / getCopilotSecurityPosture /
   scanCopilotSecurityText / listCopilotSecurityEvents`。
3. `frontend/src/views/admin/CopilotSecurityCenter.vue` —— 页面（`<script setup lang="ts">`）。
4. `frontend/src/views/admin/AdminUnifiedCenter.vue` —— 在 `operations` 域 tab 列表里、`policies` 之后加一条：
   `{ name: 'copilot-security', label: '小菱安全中心', component: CopilotSecurityCenter }`
   （需要 `defineAsyncComponent` 惰性引入，与同文件其它 tab 写法一致）。
5. `frontend/src/views/admin/CopilotSecurityCenter.test.ts` —— vitest + `@vue/test-utils`，mock `@/api/copilotSecurity`。

### 2.2 页面结构（从上到下）

1. **态势头**：大号评分 + 等级徽标（A+ 绿 / A 绿 / B 橙 / C 黄 / D·E 红）+
   `score` 进度环或进度条 + 右侧「停机开关」按钮（打开时整页顶部一条红色 `el-alert`：
   「管理小菱已紧急停机：<reason>」）+ 「重新读取」。
2. **预设区**：4 个预设卡片（`presets` 直接来自 `/center`），当前预设高亮；
   点击 → `ElMessageBox.confirm` 说明影响 → 调 `/policy/preset`；`max` 额外要求输入确认文本
   「确认最顶级」后才发 `confirm: true`。
3. **策略表单**：分组折叠面板
   - 会话防护：`session_guard_surfaces`（多选 user/admin）、`ip_allowlist`（一行一个，提交前用
     `/scan` 不校验？——CIDR 由后端校验，前端只做非空 trim）、`bind_session_to_ip`、
     `session_idle_timeout_seconds`、`session_absolute_timeout_seconds`、`max_message_chars`、
     `trust_proxy_headers`
   - 提示注入：`injection_scan_enabled`、`injection_block_threshold`（滑块 0–100）、
     `injection_warn_threshold`、`injection_blocked_surfaces`
   - 工具闸门：`tool_allowlist` / `tool_denylist`（逗号或换行分隔的 tag 输入）、`deny_shell`、
     `deny_production_config`、`deny_write_actions`、`require_approval_for_writes`、
     `dangerous_tools_require_approval`、`max_tool_calls_per_turn`、`max_tool_calls_per_hour`
   - 审计与脱敏：`audit_events_enabled`、`redaction_enabled`、`redact_in_audit`、`alert_on_block`、
     `step_up_window_seconds`、`governed_agents`
   只提交**被改动**的字段（PATCH 语义）；保存成功用返回的 `changed` 提示改了哪几项。
4. **整改清单**：`critical_gaps` 非空时列红条，逐项显示 `checks[key].title` + `remediation`；
   已通过的项折叠在「已满足」里。
5. **注入演练**：`el-input type=textarea` + 「演练」按钮 → `POST /scan`，
   展示 `score`、命中规则表（code / category / score / description / evidence）、
   脱敏后的文本（`<pre>` 保留空白）。**说明这是演练，不会落库也不会拦截**。
6. **事件列表**：`el-table` + 筛选（事件类型下拉用 `event_kinds`、状态、天数）+ 分页；
   列：时间 / 类型 / 状态（failed 红、info 灰）/ 操作者 / IP / 目标 / 详情（`el-popover` 展示 JSON）。
   顶部右侧放一个「导出」按钮位置并留 `TODO` 注释指向 codex 的 `/events/export`（本期不实现请求）。

### 2.3 约束

- 一律走 `@/api/http`，不要 `axios` 直连；错误用项目现有 `actionableError(err)` 抛 `ElMessage`。
- 写操作按钮：`!userStore.isSuperAdmin()` 时 `disabled` + `el-tooltip`「仅超级管理员可修改」。
- 页面必须能在 `/admin/operations?section=copilot-security` 直接打开并自加载。
- 不要改动后端任何文件，也不要动 `SecurityCenter.vue`（那是用户侧安全页）。

### 2.4 验收

```bash
cd frontend
npx vitest run src/views/admin/CopilotSecurityCenter.test.ts
npx vue-tsc --noEmit   # 至少确认本文件无类型错误
```

---

## 3. codex 任务：事件导出、趋势聚合、运维文档

### 3.1 服务 `backend/app/services/copilot_security_export_service.py`

只做查询与格式化，不碰策略判定：

- `export_events(db, *, kind="", status="", days=7, fmt="csv") -> tuple[str, str, str]`
  返回 `(content, media_type, filename)`；CSV 用 `csv` + `io.StringIO`（`\r\n`、UTF-8 BOM 便于 Excel），
  列固定：`id, create_time, kind, status, actor_id, actor_name, ip, target, detail`。
  JSON 格式返回 `application/json` 的数组文本。
- `build_trend(db, *, days=7) -> dict`：按天（`date(create_time)`）× 事件类型聚合，
  返回 `{窗口: days, 日期轴: [YYYY-MM-DD...], 序列: [{kind, label, points:[int...]}], 合计: {...}}`，
  缺口补 0，日期轴连续。
- `summarize_by_kind(db, *, days=7) -> list[dict]`：`{kind, label, total, blocked, last_seen}`，
  `label` 复用 `copilot_security_service.EVENT_KINDS`。
- 一律过滤 `AuditLog.action LIKE 'copilot_security.%'`；SQLite 与 MySQL 都要能跑
  （用 `func.date()` 而不是数据库特有的日期函数）。

### 3.2 路由 `backend/app/api/v1/copilot_security_analytics.py`

前缀同样是 `/admin/copilot-security`，权限 `Depends(require_admin)` + `require_permission(SecurityView)`：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/events/export?format=csv\|json&days=7&kind=&status=` | `StreamingResponse`，`Content-Disposition: attachment; filename="copilot-security-events-YYYYMMDD.csv"` |
| GET | `/events/trend?days=7` | `Resp[dict]`，含 `trend` 与 `by_kind` |

注册方式：在 `backend/app/api/__init__.py` 的 import 块加 `copilot_security_analytics`（按字母序，
放在 `copilot_security` 之后），并在 `copilot_security` 的 `include_router` 之后加一行
`api_router.include_router(copilot_security_analytics.router, prefix="/admin/copilot-security", tags=["小菱安全中心"])`。

**路由基线必须同步更新**（否则 `tests/test_complete_permission_matrix.py` 会红）：
新增 2 条路由 + 1 个新源文件，请按实测值改这 5 个数字
（`len(rows)`、唯一路由数、`len({row["source"]})`、`AUTHENTICATED_ROUTES`、`GUARDED_ROUTES`），
并在 `expected_new_routes` 里加这两条：

```python
("GET", "/api/admin/copilot-security/events/export"): ["require_admin", "security:view"],
("GET", "/api/admin/copilot-security/events/trend"): ["require_admin", "security:view"],
```

### 3.3 文档

`docs/小菱安全中心/` 下补两篇（中文，Markdown，附图可省）：

1. `02-使用手册.md`：管理员视角——怎么切预设、怎么填 IP 白名单（含 CIDR 示例）、
   怎么配置工具白名单（列出平台常用工具编码）、怎么用演练、怎么看事件、什么时候拉停机开关。
2. `03-运维手册.md`：上线/回滚——策略存在 `system_config`（键 `copilot_security`），
   无数据库迁移；紧急处置三步（拉停机 → 看事件 → 修策略）；`max` 预设下不要清空 `ip_allowlist`；
   Nginx 反代下 `trust_proxy_headers` 的开启条件与 `X-Forwarded-For` 覆写要求。

### 3.4 验收

```bash
cd backend
.venv/bin/python -m pytest tests/test_copilot_security_analytics.py -q --no-cov
.venv/bin/python -m pytest tests/test_complete_permission_matrix.py -q --no-cov
```

---

## 4. 变更纪律

- 提交前 `git status` 只应出现自己范围内的文件。
- 不要格式化整片文件（会让 diff 爆炸）；只做最小必要改动。
- 任何一条断言/基线数字改动，必须在本文档底部「变更记录」加一行说明原因。

## 变更记录

- 2026-11-17：新增事件导出与趋势两个受限路由后，权限矩阵按实测值从 341 条/43 个源文件/327 条认证路由/260 条守卫路由更新为 343 条/44 个源文件/329 条认证路由/262 条守卫路由。

---

## 5. 附：真实工具编码清单（配置 `tool_allowlist` 时照抄）

来自 `backend/app/agents/tool_contracts.py` 的 `FixedToolContract` 注册表（53 条），
写"最小权限白名单"时请用**这里的确切编码**，不要自造：

管理侧只读（建议放进白名单）：

```
admin_list_users, admin_list_roles, admin_governance_overview, admin_list_agents,
admin_list_approvals, admin_list_agent_release_approvals, admin_system_status
```

平台侧只读：

```
list_projects, list_review_tasks, list_review_issues, list_code_files, list_rules,
list_reports, dashboard_summary, detect_language, list_agent_skills
```

写/高危（`max` 预设下会转审批或被拒，按需决定是否放行）：

```
admin_set_user_role, admin_delete_user, admin_delete_users, admin_toggle_agent,
admin_decide_agent_release, create_project, delete_project, start_review,
close_sandbox, extend_sandbox, trigger_evolution, analyze_project, review_code,
audit_security_for_file, audit_security_for_task
```

> `classify_action` 判定口径：单词词元精确匹配（`settings` 不会命中 `set`），
> 含 `_`/`:` 的复合词做子串匹配（`system_config` 能命中 `system_config:update`）。
> 所以 `admin_set_user_role` 同时命中 `write` + `production`，在 `max` 预设下直接被拒——
> 这是有意的：改角色/权限必须人去后台点，不走 Agent。

---

## 6. 变更记录

| 日期 | 变更 | 原因 |
| --- | --- | --- |
| 2026-11-17 | 路由基线 333 → 341 → 343（源文件 43 → 44，认证 327 → 329，守卫 260 → 262） | 小菱安全中心 8 条 + 导出/趋势 2 条新路由，逐条复核守卫后更新 |
| 2026-11-17 | `ADMIN_CAPABILITIES` 127 → 135 | 安全中心页面调用的 8 个接口纳入管理能力注册表；写/停机三类走 `server_ops:critical`（等价超级管理员） |
| 2026-11-17 | 默认策略改为"只检测不硬拦"，`governed_agents` 补 `operations` / `orchestrator` | 小菱在网关上以 `chat_assistant` / `operations` 出现；默认硬拦会掐断既有管理流程，收紧交给 preset |
| 2026-11-17 | `/api/ai/chat` 也接入入站守卫 | 旧入口不能成为绕过点（该路径没有工具网关，只有入站守卫） |
| 2026-11-17 | 守卫改为 `Depends(optional_request)` 可选依赖 | 既有单测直接调用端点函数，不能因为新增参数而破坏签名 |
| 2026-11-17 | 更新 121 计费证据镜像指纹（`api/__init__.py`、`core/dependencies.py`） | 本次发布只在这两个被 pin 的模块里加了"注册小菱安全中心路由"和"可选 Request 依赖"，未触碰任何计费/证据读取逻辑；按 `deploy/lib/billing_evidence_compatibility.py` 的约定，评审后同步指纹 |
| 2026-11-17 | 红队复核后补齐 B1~B5 五处绕过 | 见 `fix(security): 按红队复核补齐小菱安全闸门的五处绕过` 提交；管理面强制服务端历史、工具输出回流扫描、会话绑定改存不脱敏列、新增 trusted_proxies、风险词表补洞 + 未声明工具转审批 |
