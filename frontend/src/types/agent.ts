export interface AgentProfileOut {
  code: string
  name: string
  focus: string
  issue_types: string[]
  instruction: string
  enabled: boolean
}

export interface AgentUsageOut {
  code: string
  name: string
  call_count: number
  success_count: number
  failed_count: number
  last_called_at?: string | null
}

export interface ReviewTypeMappingOut {
  review_type: string
  label: string
  agent_codes: string[]
}

export interface AgentOverviewOut {
  agents: AgentProfileOut[]
  type_mappings: ReviewTypeMappingOut[]
  usage: AgentUsageOut[]
}

// === v2.0 新增 ===

export type AgentStatus =
  | 'idle' | 'thinking' | 'working' | 'blocked' | 'error' | 'offline'

export interface AgentRuntimeOut {
  code: string
  name: string
  description: string
  icon: string
  color: string
  category: string
  skills: string[]
  status: AgentStatus
  model: string
  call_count: number
  success_count: number
  failed_count: number
  last_called_at?: string | null
}

export interface AgentRuntimeSummaryOut {
  total: number
  by_category: { category: string; count: number }[]
}

export interface AgentSituationOut {
  online: number
  working: number
  idle: number
  today_calls: number
  spectrum: { bucket: string; count: number }[]
  hotspots: { code: string; name: string; count: number }[]
}
