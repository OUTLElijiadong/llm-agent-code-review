/**
 * SecuritySentinel Agent 类型定义 (v2.1)
 * 与 backend/app/schemas/security.py 一一对应
 */

export interface SecurityFindingOut {
  title: string
  category: string
  owasp: string
  cwe: string
  severity: string
  file_path: string
  file_id?: number | null
  lines: string
  line_number: number
  end_line: number
  evidence: string
  exploit_scenario: string
  fix_suggestion: string
  references: string[]
  confidence: number
  source: string
}

export interface DataFlowOut {
  from: string
  via: string[]
  to: string
  risk_type: string
  severity: string
}

export interface ApiEndpointOut {
  method: string
  path: string
  file_path: string
  line_number: number
  handler: string
  auth_hint: string
  source: string
}

export interface CodeLinkOut {
  from: string
  to: string
  relation: string
  risk_type: string
  severity: string
}

export interface EntryPointOut {
  file: string
  function: string
  line: number
  risk: string
}

export interface ThreatModelOut {
  entry_points: EntryPointOut[]
  data_flows: DataFlowOut[]
  api_endpoints: ApiEndpointOut[]
  code_links: CodeLinkOut[]
  attack_surface_summary: string
}

export interface DiscussionTurnOut {
  agent_code: string
  agent_name: string
  role: string
  content: string
}

export interface SecurityDiscussionOut {
  mode: string
  participants: string[]
  turns: DiscussionTurnOut[]
  consensus: string
  action_items: string[]
}

export interface SecurityScanOut {
  findings: SecurityFindingOut[]
  threat_model: ThreatModelOut | null
  discussion: SecurityDiscussionOut | null
  compliance: Record<string, unknown>
  risk_score: number
  summary: string
  file_count: number
  duration_ms: number
}

export interface SecurityScanFileIn {
  file_id: number
  scan_depth?: 'quick' | 'standard' | 'deep'
}

export interface SecurityScanTaskIn {
  task_id: number
}

export interface SecurityScanProjectIn {
  project_id: number
  top_n?: number
  trace_dataflow?: boolean
}

export interface SecurityScanAllProjectsIn {
  top_n_per_project?: number
  trace_dataflow?: boolean
}

export interface SecurityChecklistItem {
  code: string
  name: string
  owasp: string
  cwe: string
  description: string
}

export interface SecurityChecklistOut {
  owasp_top10: SecurityChecklistItem[]
  secret_patterns: SecurityChecklistItem[]
  static_rules: SecurityChecklistItem[]
}

// ---- v2.1.1 Dashboard 态势汇总 ----

export interface OwaspHotspotOut {
  owasp: string
  count: number
}

export interface RiskyProjectOut {
  project_id: number
  project_name: string
  risk_score: number | null
  severe_issues: number
  high_issues: number
}

export interface TrendPointOut {
  date: string
  severe: number
  high: number
}

export interface SecurityDashboardSummaryOut {
  user_scope: string
  project_count: number
  scanned_project_count: number
  avg_risk_score: number | null
  severe_issues_total: number
  high_issues_total: number
  medium_issues_total: number
  low_issues_total: number
  owasp_hotspots: OwaspHotspotOut[]
  top_risky_projects: RiskyProjectOut[]
  trend: TrendPointOut[]
}
