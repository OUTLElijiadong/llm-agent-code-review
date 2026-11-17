/**
 * 小菱安全中心类型定义
 * 与 backend/app/schemas/copilot_security.py 一一对应（接口名保留后端 Out/In 命名）。
 *
 * 注意：refs 只提供了 API 路由文件 backend/app/api/v1/copilot_security.py，
 * schema 源文件未随交接材料提供，字段集合按契约第 1.2 / 2.2 节与路由中的
 * 响应拼装推导，31 个策略字段以 CopilotSecurityPolicyOut 为准。
 */

/** 策略生效的会话面：用户端 / 管理端。 */
export type SessionSurface = 'user' | 'admin'

/** 四档预设编码。 */
export type CopilotSecurityPreset = 'monitor' | 'balanced' | 'strict' | 'max'

/** 态势等级。 */
export type PostureGrade = 'A+' | 'A' | 'B' | 'C' | 'D' | 'E'

/** 策略全部字段（31 个）。GET /policy 与 /center 的 policy 都是这一份。 */
export interface CopilotSecurityPolicyOut {
  preset: CopilotSecurityPreset
  /** 紧急停机：打开后管理小菱的一切请求与工具调用立即被拒。 */
  kill_switch: boolean
  kill_switch_reason: string

  // —— 会话防护 ——
  /** 会话防护生效的入口面。 */
  session_guard_surfaces: SessionSurface[]
  /** 允许访问的 IP / CIDR，一行一个，合法性由后端校验。 */
  ip_allowlist: string[]
  /** 会话绑定首次来源 IP，中途换 IP 即拒绝。 */
  bind_session_to_ip: boolean
  /** 空闲超时（秒），0 表示不限。 */
  session_idle_timeout_seconds: number
  /** 绝对超时（秒），0 表示不限。 */
  session_absolute_timeout_seconds: number
  /** 单条消息字符上限，0 表示不限。 */
  max_message_chars: number
  /** 是否信任反向代理转发的客户端 IP 头。 */
  trust_proxy_headers: boolean

  // —— 提示注入 ——
  injection_scan_enabled: boolean
  /** 达到该分值即拦截，0–100。 */
  injection_block_threshold: number
  /** 达到该分值只告警不拦截，0–100。 */
  injection_warn_threshold: number
  /** 注入拦截生效的入口面。 */
  injection_blocked_surfaces: SessionSurface[]

  // —— 工具闸门 ——
  /** 允许调用的工具编码；非空时其余工具一律拒绝。 */
  tool_allowlist: string[]
  /** 明确禁止的工具编码。 */
  tool_denylist: string[]
  /** 禁止 shell 类工具。 */
  deny_shell: boolean
  /** 禁止改生产配置与凭据。 */
  deny_production_config: boolean
  /** 禁止一切写操作。 */
  deny_write_actions: boolean
  /** 写操作不直接执行，转人工审批。 */
  require_approval_for_writes: boolean
  /** 危险工具必须审批。 */
  dangerous_tools_require_approval: boolean
  /** 单轮工具调用上限，0 表示不限。 */
  max_tool_calls_per_turn: number
  /** 每小时工具调用上限，0 表示不限。 */
  max_tool_calls_per_hour: number

  // —— 审计与脱敏 ——
  audit_events_enabled: boolean
  /** 工具日志落库前脱敏。 */
  redaction_enabled: boolean
  /** 审计事件详情同样脱敏。 */
  redact_in_audit: boolean
  /** 拦截时同时生成治理告警。 */
  alert_on_block: boolean
  /** 二次确认有效窗口（秒）。 */
  step_up_window_seconds: number
  /** 策略总开关：关闭后所有闸门失效（仅事故期短开）。 */
  enabled: boolean
  /** 当前模式，随预设变化；只读展示。 */
  mode: CopilotSecurityPreset
  /** 需要脱敏的字段类别；空数组表示全部类别都脱敏。 */
  redaction_kinds: string[]
  /** 受本策略治理的 Agent 编码。 */
  governed_agents: string[]

}

/** 31 个策略字段名，保存时按它做 PATCH 差量。 */
export const COPILOT_SECURITY_POLICY_KEYS = [
  'preset',
  'enabled',
  'redaction_kinds',
  'kill_switch',
  'kill_switch_reason',
  'session_guard_surfaces',
  'ip_allowlist',
  'bind_session_to_ip',
  'session_idle_timeout_seconds',
  'session_absolute_timeout_seconds',
  'max_message_chars',
  'trust_proxy_headers',
  'injection_scan_enabled',
  'injection_block_threshold',
  'injection_warn_threshold',
  'injection_blocked_surfaces',
  'tool_allowlist',
  'tool_denylist',
  'deny_shell',
  'deny_production_config',
  'deny_write_actions',
  'require_approval_for_writes',
  'dangerous_tools_require_approval',
  'max_tool_calls_per_turn',
  'max_tool_calls_per_hour',
  'audit_events_enabled',
  'redaction_enabled',
  'redact_in_audit',
  'alert_on_block',
  'step_up_window_seconds',
  'governed_agents',
] as const satisfies readonly (keyof CopilotSecurityPolicyOut)[]

/** PUT /policy 的请求体：只含被改动的字段。 */
export type CopilotSecurityPolicyIn = Partial<CopilotSecurityPolicyOut>

/** POST /policy/preset 的请求体；切 max 必须 confirm=true。 */
export interface CopilotSecurityPresetIn {
  preset: CopilotSecurityPreset
  confirm: boolean
}

/** POST /kill-switch 的请求体。 */
export interface CopilotSecurityKillSwitchIn {
  enabled: boolean
  reason?: string
}

/** 单个态势检查项。 */
export interface CopilotSecurityCheckOut {
  key: string
  title: string
  weight: number
  critical: boolean
  passed: boolean
  remediation: string
}

/** 态势里附带的近期事件统计。 */
export interface CopilotSecurityPostureEventsOut {
  window_hours: number
  total: number
  blocked: number
  by_action: Record<string, number>
}

/** GET /posture 的响应，也嵌在 /center 与保存结果里。 */
export interface CopilotSecurityPostureOut {
  score: number
  grade: PostureGrade
  checks: CopilotSecurityCheckOut[]
  recommendations: string[]
  critical_gaps: string[]
  events: CopilotSecurityPostureEventsOut
  policy: CopilotSecurityPolicyOut
  generated_at: string
}

/** 预设目录项。 */
export interface PresetOptionOut {
  preset: CopilotSecurityPreset
  label: string
}

/** 事件类型目录项。 */
export interface EventKindOptionOut {
  kind: string
  label: string
}

/** GET /center 的响应：一次拿全。 */
export interface CopilotSecurityCenterOut {
  policy: CopilotSecurityPolicyOut
  posture: CopilotSecurityPostureOut
  presets: PresetOptionOut[]
  event_kinds: EventKindOptionOut[]
  governed_agent: string
}

/** 保存类接口的统一响应：最新策略 + 实际改动的字段 + 最新态势。 */
export interface CopilotSecurityPolicySavedOut {
  policy: CopilotSecurityPolicyOut
  changed: string[]
  posture: CopilotSecurityPostureOut
}

/** 注入命中的一条规则。 */
export interface CopilotSecurityInjectionHitOut {
  code: string
  category: string
  score: number
  description: string
  evidence: string
}

/** 注入检测结果。 */
export interface CopilotSecurityInjectionOut {
  score: number
  hits: CopilotSecurityInjectionHitOut[]
  obfuscation?: Record<string, unknown>
}

/** 一条脱敏命中。 */
export interface CopilotSecurityRedactionHitOut {
  kind: string
  count: number
}

/** POST /scan 的请求体。 */
export interface CopilotSecurityScanIn {
  text: string
}

/** POST /scan 的响应；演练不落库、不拦截。 */
export interface CopilotSecurityScanOut {
  injection: CopilotSecurityInjectionOut
  redacted: string
  redaction_hits: CopilotSecurityRedactionHitOut[]
  original_length: number
}

/** 一条安全事件（来自 audit_log 的 copilot_security.* 记录）。 */
export interface CopilotSecurityEventOut {
  id: number
  create_time: string
  kind: string
  status: string
  actor_id: number | null
  actor_name: string
  ip: string
  target: string
  detail: Record<string, unknown> | string | null
}

/** GET /events 的查询参数。 */
export interface CopilotSecurityEventQuery {
  page?: number
  page_size?: number
  kind?: string
  status?: string
  days?: number
}

/** GET /events 的响应。 */
export interface CopilotSecurityEventListOut {
  items: CopilotSecurityEventOut[]
  total: number
  page: number
  page_size: number
  pages: number
}

// ── 小菱安全运营（自动化攻防）────────────────────────────────────────────────

/** 归一化后的攻击源画像。 */
export interface SecOpsSourceOut {
  ip: string
  hits: number
  sensors: string[]
  kinds: string[]
  severity: 'critical' | 'high' | 'medium' | 'low'
  geo: Record<string, unknown>
  last_seen: string | null
  recommended: 'block' | 'watch'
}

export interface SecOpsSensorOut {
  action: string
  ok: boolean
  events: number
  error: string | null
}

export interface SecOpsDigestOut {
  window_hours: number
  generated_at: string
  sensors: SecOpsSensorOut[]
  totals: { sources: number; critical: number; high: number }
  sources: SecOpsSourceOut[]
  recommendations: string[]
}

export interface SecOpsBaselineCheckOut {
  key: string
  title: string
  passed: boolean | null
  detail: string
  remediation: string
  weight: number
}

export interface SecOpsBaselineOut {
  generated_at: string
  base_url: string | null
  score: number
  checks: SecOpsBaselineCheckOut[]
  failed: string[]
  remediations: string[]
}

export interface SecOpsCandidatesOut {
  available: boolean
  candidates: Record<string, unknown>[]
  raw: Record<string, unknown> | null
}

export interface SecOpsPatrolOut {
  auto_block_enabled: boolean
  patrol_enabled: boolean
  public_base_url: string | null
  asset_hosts: string[]
  last_run: Record<string, unknown>
  recent: Record<string, unknown>[]
}

export interface SecOpsBlockOut {
  ip: string
  duration_seconds: number
  blocked: boolean
  released: boolean
  result: Record<string, unknown>
  configure: Record<string, unknown>
}
