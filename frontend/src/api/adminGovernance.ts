import { get, post, put } from './http'
import type {
  AgentAlert,
  AgentArtifactVersion,
  AgentJob,
  AgentKnowledgeDoc,
  AgentKnowledgeSource,
  AgentMemory,
  AgentRewardEvent,
  AgentToolPermission,
  ApprovalItem,
  GovernanceAgent,
  GovernanceOverview,
  PolicyDecision,
  PolicyRule,
  PolicyRuleInput,
  ToolCallLog,
} from '@/types/adminGovernance'

export function getGovernanceOverview(): Promise<GovernanceOverview> {
  return get<GovernanceOverview>('/admin/governance/overview')
}

export function listGovernanceAgents(): Promise<GovernanceAgent[]> {
  return get<GovernanceAgent[]>('/admin/governance/agents')
}

export function updateGovernanceAgent(
  code: string,
  data: Partial<Pick<GovernanceAgent, 'status' | 'budget_tokens_daily' | 'priority' | 'auto_approval_threshold' | 'is_enabled'>>,
): Promise<GovernanceAgent> {
  return put<GovernanceAgent>(`/admin/governance/agents/${code}`, data)
}

export function listAgentMemory(code: string): Promise<AgentMemory[]> {
  return get<AgentMemory[]>(`/admin/governance/agents/${code}/memory`)
}

export function createAgentMemory(
  code: string,
  data: { title: string; content: string; memory_type?: string; weight?: number; source_ref?: string },
): Promise<AgentMemory> {
  return post<AgentMemory>(`/admin/governance/agents/${code}/memory`, data)
}

export function listAgentKnowledge(code: string): Promise<AgentKnowledgeDoc[]> {
  return get<AgentKnowledgeDoc[]>(`/admin/governance/agents/${code}/knowledge`)
}

export function createAgentKnowledgeDoc(data: {
  agent_code: string
  title: string
  content: string
  source_type?: string
  source_ref?: string
  risk_level?: string
  confidence?: number
}): Promise<AgentKnowledgeDoc> {
  return post<AgentKnowledgeDoc>('/admin/governance/knowledge/docs', data)
}

export function activateAgentKnowledgeDoc(id: number): Promise<AgentKnowledgeDoc> {
  return post<AgentKnowledgeDoc>(`/admin/governance/knowledge/docs/${id}/activate`)
}

export function listAgentKnowledgeSources(agentCode = ''): Promise<AgentKnowledgeSource[]> {
  return get<AgentKnowledgeSource[]>('/admin/governance/knowledge/sources', { agent_code: agentCode })
}

export function upsertAgentKnowledgeSource(
  data: Omit<AgentKnowledgeSource, 'id' | 'create_time'>,
  sourceId = 0,
): Promise<AgentKnowledgeSource> {
  return post<AgentKnowledgeSource>('/admin/governance/knowledge/sources', data, { source_id: sourceId })
}

export function crawlAgentKnowledgeSources(agentCode = ''): Promise<Record<string, unknown>> {
  return post<Record<string, unknown>>('/admin/governance/knowledge/crawl', {}, { agent_code: agentCode })
}

export function listApprovals(status = ''): Promise<ApprovalItem[]> {
  return get<ApprovalItem[]>('/admin/approvals', { status })
}

export function approveItem(id: number, note = ''): Promise<ApprovalItem> {
  return post<ApprovalItem>(`/admin/approvals/${id}/approve`, { note })
}

export function rejectItem(id: number, note = ''): Promise<ApprovalItem> {
  return post<ApprovalItem>(`/admin/approvals/${id}/reject`, { note })
}

export function listPolicies(): Promise<PolicyRule[]> {
  return get<PolicyRule[]>('/admin/policies')
}

export function upsertPolicy(data: PolicyRuleInput, ruleId = 0): Promise<PolicyRule> {
  return post<PolicyRule>('/admin/policies', data, { rule_id: ruleId })
}

export function evaluatePolicy(data: { subject: string; action: string; resource: string; context?: Record<string, unknown> }): Promise<PolicyDecision> {
  return post<PolicyDecision>('/admin/policies/evaluate', data)
}

export function listPolicyDecisions(): Promise<PolicyDecision[]> {
  return get<PolicyDecision[]>('/admin/policies/decisions')
}

export function listToolCalls(): Promise<ToolCallLog[]> {
  return get<ToolCallLog[]>('/admin/tools/calls')
}

export function listToolPermissions(): Promise<AgentToolPermission[]> {
  return get<AgentToolPermission[]>('/admin/tools/permissions')
}

export function upsertToolPermission(
  data: Omit<AgentToolPermission, 'id' | 'create_time'>,
  permissionId = 0,
): Promise<AgentToolPermission> {
  return post<AgentToolPermission>('/admin/tools/permissions', data, { permission_id: permissionId })
}

export function listJobs(): Promise<AgentJob[]> {
  return get<AgentJob[]>('/admin/jobs')
}

export function runJob(id: number): Promise<Record<string, unknown>> {
  return post<Record<string, unknown>>(`/admin/jobs/${id}/run`)
}

export function updateJob(id: number, data: { schedule?: string; status?: string; config_json?: Record<string, unknown> }): Promise<AgentJob> {
  return put<AgentJob>(`/admin/jobs/${id}`, data)
}

export function getObservabilityOverview(): Promise<Record<string, unknown>> {
  return get<Record<string, unknown>>('/admin/observability/overview')
}

export function listAlerts(status = 'open'): Promise<AgentAlert[]> {
  return get<AgentAlert[]>('/admin/observability/alerts', { status })
}

export function resolveAlert(id: number, note = ''): Promise<AgentAlert> {
  return post<AgentAlert>(`/admin/observability/alerts/${id}/resolve`, { note })
}

export function listRewardEvents(): Promise<AgentRewardEvent[]> {
  return get<AgentRewardEvent[]>('/admin/rewards/events')
}

export function createRewardEvent(data: {
  agent_code: string
  event_type: string
  score: number
  reason: string
  impact?: Record<string, unknown>
}): Promise<AgentRewardEvent> {
  return post<AgentRewardEvent>('/admin/rewards/events', data)
}

export function listArtifactVersions(params: { agent_code?: string; artifact_type?: string } = {}): Promise<AgentArtifactVersion[]> {
  return get<AgentArtifactVersion[]>('/admin/rollback/versions', params)
}

export function createArtifactVersion(data: {
  agent_code: string
  artifact_type: string
  version: string
  content: string
  snapshot?: string
  status?: string
}): Promise<AgentArtifactVersion> {
  return post<AgentArtifactVersion>('/admin/rollback/versions', data)
}

export function rollbackArtifactVersion(id: number): Promise<AgentArtifactVersion> {
  return post<AgentArtifactVersion>(`/admin/rollback/versions/${id}/rollback`)
}
