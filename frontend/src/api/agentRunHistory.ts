import { get } from './http'
import { getPersonalAgentRun, type PersonalAgentRun } from './personalAgents'
import { getPublicAgentRun } from './personalPublications'
import { getEnterpriseAssetRun } from './enterpriseAssetRuns'

export type AgentRunSource = 'private' | 'public' | 'enterprise_public'
export interface AgentRunSummary {
  id: number
  agent_id: number | null
  publication_id: number | null
  enterprise_publication_id?: number | null
  version_number: number
  title: string
  status: PersonalAgentRun['status']
  actual_milli: number | null
  created_at: string
}
export const listAgentRuns = (source: AgentRunSource, token: string, beforeId?: number) =>
  get<AgentRunSummary[]>('/me/agent-runs', { source, before_id: beforeId, limit: 20 }, undefined, false, { authTokenSnapshot: token })
export const getAgentRun = (row: AgentRunSummary, token: string) => {
  if (row.enterprise_publication_id != null) return getEnterpriseAssetRun(row.enterprise_publication_id, row.id, token)
  return row.publication_id !== null
    ? getPublicAgentRun(row.publication_id, row.id, token)
    : getPersonalAgentRun(row.agent_id!, row.id, token)
}
