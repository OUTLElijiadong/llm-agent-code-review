/**
 * 管理员总览大屏聚合 API
 * 服务器状态 / 安全态势 / 登录来源地图 / Agent 活跃状态
 */
import { get } from './http'

/** 服务器运行状态 */
export interface SystemStatus {
  available: boolean
  collected_at: string
  process_uptime_seconds: number
  cpu_percent?: number
  memory_percent?: number
  memory_used_mb?: number
  memory_total_mb?: number
  disk_percent?: number
  disk_used_gb?: number
  disk_total_gb?: number
  load_avg?: number[] | null
  uptime_seconds?: number
}

/** 安全态势 */
export interface SecurityPosture {
  level: 'ok' | 'suspicious' | 'attack'
  signals: Array<{ type: string; severity: string; title: string; detail: string }>
  brute_force_ips: Array<{ ip: string; fails: number }>
  login_failed_24h: number
  login_success_24h: number
  malware_infected_total: number
  malware_infected_24h: number
  top_login_ips: Array<{ ip: string; count: number }>
  note: string
  collected_at: string
}

/** 登录来源地理点 */
export interface GeoPoint {
  ip: string
  country?: string
  city?: string
  latitude: number
  longitude: number
  count: number
}

/** Agent 活跃状态 */
export interface AgentActivity {
  agent_code: string
  name: string
  status: 'idle' | 'working' | 'error' | 'disabled'
  calls_today: number
  purpose: string
  is_enabled: number
}

export function getSystemStatus(): Promise<SystemStatus> {
  return get<SystemStatus>('/admin/overview/system')
}

export function getSecurityPosture(): Promise<SecurityPosture> {
  return get<SecurityPosture>('/admin/overview/security')
}

export function getLoginGeo(): Promise<GeoPoint[]> {
  return get<GeoPoint[]>('/admin/overview/geo')
}

export function getAgentsActivity(): Promise<AgentActivity[]> {
  return get<AgentActivity[]>('/admin/overview/agents-activity')
}
