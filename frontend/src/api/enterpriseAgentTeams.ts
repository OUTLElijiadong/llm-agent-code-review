import http, { get, post } from './http'

export type EnterpriseAgentTeamStatus =
  | 'queued' | 'running' | 'awaiting_reconciliation' | 'completed' | 'failed' | 'cancelled' | 'suppressed'
  | string

export interface EnterpriseAgentTeamSummary {
  team_id: number
  enterprise_id: number
  conversation_id: number
  group_id: number | null
  initiator_user_id: number
  title: string | null
  status: EnterpriseAgentTeamStatus
  task_count: number
  completed_task_count: number
  running_task_count: number
  blocked_task_count: number
  created_at: string | null
  started_at: string | null
  completed_at: string | null
  can_read_conversation: boolean
}

export interface EnterpriseAgentTeamTaskResult {
  status?: string
  summary?: string
  [key: string]: unknown
}

export interface EnterpriseAgentTeamTask {
  task_id: number
  task_key: string
  title: string
  agent_code: string
  agent_name: string
  status: string
  attempt_count: number
  result: EnterpriseAgentTeamTaskResult
  errors: Array<Record<string, unknown>>
  started_at: string | null
  completed_at: string | null
}

export interface EnterpriseAgentTeamDetail {
  team_id: number
  enterprise_id: number
  conversation_id: number
  group_id: number | null
  title: string
  objective: string
  status: EnterpriseAgentTeamStatus
  created_at: string | null
  started_at: string | null
  completed_at: string | null
  tasks: EnterpriseAgentTeamTask[]
}

export interface EnterpriseTeamSecurityArtifact {
  artifact_id: number
  team_id: number
  task_id: number
  attempt_no: number
  lease_generation: number
  enterprise_id: number
  project_id: number
  audit_run_id: string
  source_sha256: string
  report_sha256: string
  coverage_status: 'complete' | 'incomplete' | 'failed' | string
  termination_reason: string
  summary: Record<string, unknown>
  report: Record<string, unknown>
  created_at: string | null
}

const base = '/enterprises'
const silentCodes = [40300, 40301, 40303, 40400]
const sessionOptions = (token: string | null) => ({ authTokenSnapshot: token })

export function listEnterpriseAgentTeams(
  enterpriseId: number,
  conversationId: number,
  token: string | null,
  beforeId?: number,
) {
  return get<{ items: EnterpriseAgentTeamSummary[]; has_more: boolean; next_before_id: number | null }>(
    `${base}/${enterpriseId}/agent-teams`,
    { conversation_id: conversationId, limit: 20, ...(beforeId === undefined ? {} : { before_id: beforeId }) },
    silentCodes,
    true,
    sessionOptions(token),
  )
}

export function getEnterpriseAgentTeam(enterpriseId: number, teamId: number, token: string | null) {
  return get<EnterpriseAgentTeamDetail>(
    `${base}/${enterpriseId}/agent-teams/${teamId}`,
    undefined,
    silentCodes,
    true,
    sessionOptions(token),
  )
}

export function getEnterpriseTeamSecurityArtifact(
  enterpriseId: number,
  teamId: number,
  artifactId: number,
  token: string | null,
) {
  return get<EnterpriseTeamSecurityArtifact>(
    `${base}/${enterpriseId}/agent-teams/${teamId}/security-artifacts/${artifactId}`,
    undefined,
    silentCodes,
    true,
    sessionOptions(token),
  )
}

export async function checkEnterpriseTeamSecurityArtifactAccess(
  enterpriseId: number,
  teamId: number,
  artifactId: number,
  token: string | null,
): Promise<void> {
  await http.head(
    `${base}/${enterpriseId}/agent-teams/${teamId}/security-artifacts/${artifactId}`,
    {
      silentCodes,
      silentStatuses: [403, 404],
      silentTransportError: true,
      ...sessionOptions(token),
    },
  )
}

export function cancelEnterpriseAgentTeam(
  enterpriseId: number,
  teamId: number,
  reason: string,
  token: string | null,
) {
  return post<{ team_id: number; status: string }>(
    `${base}/${enterpriseId}/agent-teams/${teamId}/cancel`,
    { reason },
    undefined,
    silentCodes,
    true,
    sessionOptions(token),
  )
}

export function retryEnterpriseAgentTeamTask(
  enterpriseId: number,
  teamId: number,
  taskId: number,
  token: string | null,
) {
  return post<{ team_id: number; task_id: number; status: string }>(
    `${base}/${enterpriseId}/agent-teams/${teamId}/retry`,
    { task_id: taskId },
    undefined,
    silentCodes,
    true,
    sessionOptions(token),
  )
}
