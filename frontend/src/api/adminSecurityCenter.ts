import { get, post, put } from './http'

export interface SecurityMonitorPolicy {
  ssh_failed_threshold: number
  ssh_window_hours: number
  nginx_failure_threshold: number
  nginx_window_hours: number
  popup_min_severity: 'info' | 'warning' | 'high' | 'critical'
  monitoring_mode: 'monitor_only' | 'monitor_and_block'
  automatic_blocking_enabled: boolean
  automatic_blocking_available?: boolean
  automatic_blocking_confirmed_at?: string | null
  automatic_blocking_state_source?: 'audited_receipt'
  counterattack_enabled: false
  source: string
  revision: number
  updated_at: string | null
  updated_by: string | null
  baseline: Pick<SecurityMonitorPolicy, 'ssh_failed_threshold' | 'ssh_window_hours' | 'nginx_failure_threshold' | 'nginx_window_hours' | 'popup_min_severity'>
}

export interface SecurityCenterOverview {
  generated_at: string
  monitoring: {
    enabled: boolean
    schedule: string | null
    schedule_label: string
    interval_minutes: number | null
    last_run: {
      status: string
      started_at: string | null
      finished_at: string | null
      completed_sources: number
      failed_sources: number
      degraded_sources: number
      degraded: boolean
    }
    sources: Array<{ code: string; label: string; status: 'success' | 'failed' | 'degraded' | 'unknown' }>
  }
  policy: SecurityMonitorPolicy
  open_alerts_24h: number
  open_alerts_total: number
}

export interface SecurityCenterEvent {
  id: string
  alert_id?: number
  recorded_at: string | null
  event_type: 'alert' | 'collector' | 'monitor_run' | 'policy_change' | string
  layer: string
  severity: string
  status: string
  actor: string
  title: string
  action_code?: string
  summary: string
  evidence_summary: Record<string, unknown>
  resolution?: { note: string; resolved_by_name?: string | null; resolved_at?: string | null } | null
}

export type SecurityCenterEventGroup = 'all' | 'activity' | 'inspection'

export interface SecurityCenterEventPage {
  items: SecurityCenterEvent[]
  total: number
  page: number
  page_size: number
  pages: number
  truncated: boolean
  hours: number
  event_group?: SecurityCenterEventGroup
}

export interface SecurityMonitorRunResult {
  success: boolean
  created_alerts: Array<{ alert_id: number; title: string; severity: string }>
  errors: Array<{ action: string; error: string }>
}

export function getSecurityCenterOverview(): Promise<SecurityCenterOverview> {
  return get<SecurityCenterOverview>('/admin/security-center/overview')
}

export function getSecurityCenterEvents(hours = 24, page = 1, pageSize = 20, eventGroup: SecurityCenterEventGroup = 'all'): Promise<SecurityCenterEventPage> {
  return get<SecurityCenterEventPage>('/admin/security-center/events', { hours, page, page_size: pageSize, event_group: eventGroup })
}

export function updateSecurityMonitorPolicy(
  data: Pick<SecurityMonitorPolicy, 'ssh_failed_threshold' | 'ssh_window_hours' | 'nginx_failure_threshold' | 'nginx_window_hours'>,
): Promise<SecurityMonitorPolicy> {
  return put<SecurityMonitorPolicy>('/admin/security-center/policy', data)
}

export function runSecurityMonitor(): Promise<SecurityMonitorRunResult> {
  return post<SecurityMonitorRunResult>('/admin/observability/security/run-monitor')
}

export interface AutomaticBlockingPolicy {
  enabled: boolean
  ai_anomaly_enabled: boolean
  duration_seconds: number
  window_seconds: number
  ssh_threshold: number
  web_threshold: number
  allowlist_cidrs: string[]
  /** 同一来源 24 小时内重复触发时自动延长租约（倍数递增，硬上限 3600 秒）。 */
  auto_escalate: boolean
  activated_at: string | null
}

export interface AutomaticBlockEntry {
  id: string
  ip: string
  rule: string
  evidence_count: number
  scope: string
  status: 'active' | 'expired' | 'released' | 'failed' | 'unknown'
  started_at: string | null
  expires_at: string | null
  released_at: string | null
  reason: string
  source?: string
}

export interface AutomaticBlockingSnapshot {
  available: boolean
  verified: boolean
  enabled: boolean
  policy: AutomaticBlockingPolicy
  protected_sources: Array<{ cidr: string; reason: string }>
  active_blocks: AutomaticBlockEntry[]
  recent_blocks: AutomaticBlockEntry[]
  last_evaluated_at: string | null
  errors: Array<string | Record<string, unknown>>
  backend: 'ipset'
  family_availability?: unknown
  family_support?: { ipv4: boolean; ipv6: boolean }
  request_id?: string
  outcome_unknown?: boolean
}

export type AutomaticBlockingPolicyInput = Omit<AutomaticBlockingPolicy, 'activated_at'>

export function getAutomaticBlocking(): Promise<AutomaticBlockingSnapshot> {
  return get<AutomaticBlockingSnapshot>('/admin/security-center/automatic-blocking')
}

export function updateAutomaticBlocking(data: AutomaticBlockingPolicyInput): Promise<AutomaticBlockingSnapshot> {
  return put<AutomaticBlockingSnapshot>('/admin/security-center/automatic-blocking', data)
}

export function releaseAutomaticBlock(data: { ip: string; reason: string }): Promise<AutomaticBlockingSnapshot> {
  return post<AutomaticBlockingSnapshot>('/admin/security-center/automatic-blocking/release', data)
}

export interface SecurityIpTraceAttribution {
  ok: boolean
  note?: string
  country?: string
  region?: string
  city?: string
  isp?: string
  org?: string
  as?: string
}

export interface SecurityIpTraceDefenseRecord {
  id: string
  ip: string
  rule: string
  source: string
  status: string
  started_at: string | null
  expires_at: string | null
  released_at: string | null
  evidence_count: number
  reason: string
}

/** 单来源被动溯源快照；risk.basis 说明评分只用本机可信日志。 */
export interface SecurityIpTrace {
  available: boolean
  verified: boolean
  kind: string
  request_id: string | null
  ip: string
  generated_at: string | null
  window_hours: number
  is_public: boolean
  is_protected: boolean
  risk: { score: number; level: 'low' | 'medium' | 'high' | 'critical' | string; reasons: string[]; basis: string }
  ssh: {
    count: number
    accounts_tried: Array<{ account: string; count: number }>
    first_seen: string | null
    last_seen: string | null
  }
  web: {
    count: number
    target_count: number
    targets: Array<{ path: string; count: number }>
    methods: Record<string, number>
    status_codes: Record<string, number>
    first_seen: string | null
    last_seen: string | null
  }
  defense_records: SecurityIpTraceDefenseRecord[]
  attribution: SecurityIpTraceAttribution
  reverse_dns: { ok: boolean; output: string; note: string }
  whois: { ok: boolean; summary: string; note: string }
  evidence_sources: string[]
  errors: string[]
}

export interface SecuritySurfaceFirewallChain {
  chain: string
  ok: boolean
  policy: string
  rules: string[]
  note: string
}

export interface SecuritySurfaceFirewallFamily {
  tool: string
  chains: SecuritySurfaceFirewallChain[]
  tools_present: string[]
}

/** 本机防御面只读审计快照：监听端口、防火墙链、加固应用与拦截现状。 */
export interface SecuritySurfaceAudit {
  available: boolean
  verified: boolean
  kind: string
  request_id: string | null
  generated_at: string | null
  listeners: Array<{ protocol: string; address: string; port: number; process: string }>
  public_listener_count: number
  firewall: Record<'ipv4' | 'ipv6', SecuritySurfaceFirewallFamily>
  applications: Array<{ name: string; purpose: string; installed: boolean }>
  ipset: { present: boolean; sets: string[] }
  blocking: { enabled: boolean; backend: string; active_leases: number }
  ssh_ports: string
  errors: string[]
}

export interface SecurityTrafficPeer {
  ip: string
  connections: number
  protocols: Record<string, number>
  peer_ports: Record<string, number>
  processes: string[]
  states: Record<string, number>
  ssh_failed_count: number
  sensitive_probe_count: number
  target_count: number
  last_seen: string | null
}

/** 流量元数据摘要；payload_captured 恒为 false：不捕获、不存储载荷。 */
export interface SecurityTrafficSummary {
  available: boolean
  verified: boolean
  kind: string
  request_id: string | null
  generated_at: string | null
  window_hours: number
  peers: SecurityTrafficPeer[]
  peer_total: number
  current_connections: number
  recent_ssh_failed_sources: number
  recent_probe_sources: number
  payload_captured: false
  note: string
  errors: string[]
}

export function traceSecurityIp(ip: string): Promise<SecurityIpTrace> {
  return post<SecurityIpTrace>('/admin/security-center/trace/ip', { ip })
}

export function getDefenseSurface(): Promise<SecuritySurfaceAudit> {
  return get<SecuritySurfaceAudit>('/admin/security-center/surface')
}

export function getTrafficSummary(sinceHours = 24): Promise<SecurityTrafficSummary> {
  return get<SecurityTrafficSummary>('/admin/security-center/traffic', { since_hours: sinceHours })
}
