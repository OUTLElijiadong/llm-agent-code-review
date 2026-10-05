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
