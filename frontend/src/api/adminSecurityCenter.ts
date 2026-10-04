import { get, post, put } from './http'

export interface SecurityMonitorPolicy {
  ssh_failed_threshold: number
  ssh_window_hours: number
  nginx_failure_threshold: number
  nginx_window_hours: number
  popup_min_severity: 'info' | 'warning' | 'high' | 'critical'
  monitoring_mode: 'monitor_only'
  automatic_blocking_enabled: false
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
      degraded: boolean
    }
    sources: Array<{ code: string; label: string; status: 'success' | 'failed' | 'unknown' }>
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

export interface SecurityCenterEventPage {
  items: SecurityCenterEvent[]
  total: number
  page: number
  page_size: number
  pages: number
  truncated: boolean
  hours: number
}

export interface SecurityMonitorRunResult {
  success: boolean
  created_alerts: Array<{ alert_id: number; title: string; severity: string }>
  errors: Array<{ action: string; error: string }>
}

export function getSecurityCenterOverview(): Promise<SecurityCenterOverview> {
  return get<SecurityCenterOverview>('/admin/security-center/overview')
}

export function getSecurityCenterEvents(hours = 24, page = 1, pageSize = 20): Promise<SecurityCenterEventPage> {
  return get<SecurityCenterEventPage>('/admin/security-center/events', { hours, page, page_size: pageSize })
}

export function updateSecurityMonitorPolicy(
  data: Pick<SecurityMonitorPolicy, 'ssh_failed_threshold' | 'ssh_window_hours' | 'nginx_failure_threshold' | 'nginx_window_hours'>,
): Promise<SecurityMonitorPolicy> {
  return put<SecurityMonitorPolicy>('/admin/security-center/policy', data)
}

export function runSecurityMonitor(): Promise<SecurityMonitorRunResult> {
  return post<SecurityMonitorRunResult>('/admin/observability/security/run-monitor')
}
