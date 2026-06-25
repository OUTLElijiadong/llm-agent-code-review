export interface GovernanceOverview {
  agents_total: number
  agents_enabled: number
  approvals_pending: number
  approvals_auto_today: number
  policy_decisions_today: number
  tool_calls_today: number
  alerts_open: number
  knowledge_docs_total: number
  memory_items_total: number
  jobs_enabled: number
  reward_score_total: number
  risk_distribution: { risk_level: string; count: number }[]
  recent_alerts: { id: number; title: string; severity: string }[]
}

export interface GovernanceAgent {
  code: string
  name: string
  description: string
  category: string
  status: string
  model?: string | null
  icon: string
  color: string
  budget_tokens_daily: number
  priority: number
  auto_approval_threshold: number
  is_enabled: number
  skills: string[]
  tool_count: number
  memory_count: number
  knowledge_count: number
  create_time?: string | null
  update_time?: string | null
}

export interface ApprovalItem {
  id: number
  title: string
  agent_code?: string | null
  action: string
  resource: string
  risk_level: string
  status: string
  decision?: string | null
  decision_reason?: string | null
  decided_by?: number | null
  decided_at?: string | null
  create_time?: string | null
}

export interface PolicyRule {
  id: number
  rule_code: string
  name: string
  subject: string
  action: string
  resource: string
  effect: string
  risk_level: string
  condition_json?: Record<string, unknown> | unknown[] | null
  priority: number
  enabled: number
}

export type PolicyRuleInput = Omit<PolicyRule, 'id'> & {
  condition_json?: Record<string, unknown> | unknown[] | null
}

export interface PolicyDecision {
  id?: number | null
  subject: string
  action: string
  resource: string
  decision: string
  risk_level: string
  risk_score: number
  reason?: string | null
  matched_rule_id?: number | null
  create_time?: string | null
}

export interface ToolCallLog {
  id: number
  agent_code: string
  tool_code: string
  action: string
  resource: string
  status: string
  risk_level: string
  decision: string
  input_summary?: string | null
  output_summary?: string | null
  error?: string | null
  duration_ms: number
  policy_decision_id?: number | null
  approval_id?: number | null
  create_time?: string | null
}

export interface AgentJob {
  id: number
  job_code: string
  job_type: string
  agent_code?: string | null
  schedule: string
  status: string
  last_run_at?: string | null
  config_json?: Record<string, unknown> | unknown[] | null
}

export interface AgentAlert {
  id: number
  alert_type: string
  severity: string
  status: string
  title: string
  create_time?: string | null
}

export interface AgentMemory {
  id: number
  agent_code: string
  memory_type: string
  title: string
  content: string
  weight: number
  status: string
  source_ref?: string | null
  create_time?: string | null
}

export interface AgentKnowledgeDoc {
  id: number
  agent_code: string
  source_type: string
  source_ref?: string | null
  title: string
  risk_level: string
  confidence: number
  status: string
  char_count: number
  chunk_count: number
  create_time?: string | null
}

export interface AgentKnowledgeSource {
  id: number
  agent_code: string
  source_type: string
  source_uri: string
  whitelist: number
  enabled: number
  config_json?: Record<string, unknown> | unknown[] | null
  create_time?: string | null
}

export interface AgentToolPermission {
  id: number
  agent_code: string
  tool_code: string
  permission: string
  risk_level: string
  enabled: number
  note?: string | null
  create_time?: string | null
}

export interface AgentRewardEvent {
  id: number
  agent_code: string
  event_type: string
  score: number
  reason?: string | null
  create_time?: string | null
}

export interface AgentArtifactVersion {
  id: number
  agent_code: string
  artifact_type: string
  version: string
  status: string
  content?: string | null
  snapshot?: string | null
  create_time?: string | null
}
