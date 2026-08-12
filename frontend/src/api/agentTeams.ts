import { get, post } from '@/api/http'

export type AgentTeamSurface = 'user' | 'admin'
export type AgentTeamStatus =
  | 'draft'
  | 'queued'
  | 'running'
  | 'verifying'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'expired'
export type AgentTeamMemberRole = 'worker' | 'verifier' | 'summarizer'
export type AgentTeamMemberStatus = 'created' | 'queued' | 'running' | 'completed' | 'failed' | 'reclaimed'
export type AgentTeamTaskStatus =
  | 'waiting_dependency'
  | 'queued'
  | 'running'
  | 'completed'
  | 'failed'
  | 'blocked'
  | 'cancelled'
  | 'dead_letter'
  | 'expired'
export type AgentTeamEventType = string

export interface AgentTeamCounts {
  total: number
  completed: number
  running: number
  queued: number
  failed: number
  blocked: number
}

export interface AgentTeamMember {
  member_id: number
  member_key: string
  display_name: string
  address: string
  kind: string
  role: AgentTeamMemberRole
  template_id?: number | null
  template_version_id?: number | null
  capabilities?: Record<string, unknown>
  status: AgentTeamMemberStatus
  started_at?: string | null
  completed_at?: string | null
  lease_expires_at?: string | null
}

export interface AgentTeamTask {
  task_id: number
  task_key: string
  member_id: number
  member_key?: string
  title: string
  instructions?: string
  depends_on: string[]
  input?: Record<string, unknown>
  status: AgentTeamTaskStatus
  priority: number
  attempt_count: number
  max_attempts: number
  next_attempt_at?: string | null
  started_at?: string | null
  completed_at?: string | null
  result?: Record<string, unknown> | null
  artifacts?: Array<Record<string, unknown>>
  errors?: Array<Record<string, unknown>>
}

export interface AgentTeamEvent {
  event_id: number
  team_id: number
  task_id?: number | null
  member_id?: number | null
  message_id?: string | null
  correlation_id?: string
  event_type: AgentTeamEventType
  from_status?: string | null
  to_status?: string | null
  actor_address?: string
  trace_id?: string
  detail?: Record<string, unknown>
  created_at?: string
}

export interface AgentTeamMessage {
  ledger_id?: number
  message_id: string
  trace_id: string
  correlation_id: string
  causation_id?: string
  sent_from: string
  send_to: string
  message_type: string
  subject: string
  status: string
  payload?: Record<string, unknown>
  context?: Record<string, unknown>
  artifacts?: Array<Record<string, unknown>>
  errors?: Array<Record<string, unknown>>
  create_time?: string
  update_time?: string
  created_at?: string
}

export interface AgentTeamMessagePage {
  items: AgentTeamMessage[]
  total: number
  has_more: boolean
  next_before_id?: number | null
  page_size: number
}

export type AgentTeamMessagePageMeta = Omit<AgentTeamMessagePage, 'items'>

export interface AgentTeamSummary {
  team_id: number
  title: string
  objective?: string
  surface: AgentTeamSurface
  session_id: string
  status: AgentTeamStatus
  max_active_children: number
  trace_id: string
  counts?: AgentTeamCounts
  user_id?: number
  max_attempts?: number
  priority?: number
  deadline_at?: string | null
  summary?: Record<string, unknown>
  error?: Record<string, unknown>
  created_at?: string
  updated_at?: string
  started_at?: string | null
  completed_at?: string | null
  archived_at?: string | null
}

export interface AgentTeamDetail extends AgentTeamSummary {
  members: AgentTeamMember[]
  tasks: AgentTeamTask[]
  events: AgentTeamEvent[]
  messages: AgentTeamMessage[]
  message_page?: AgentTeamMessagePageMeta
}

export interface AgentTeamListQuery {
  surface?: AgentTeamSurface
  session_id?: string
  status?: AgentTeamStatus
  limit?: number
}

export interface AgentTeamListResponse {
  items: AgentTeamSummary[]
  total: number
  page?: number
  page_size?: number
}

export interface AgentTeamMemberInput {
  member_key: string
  display_name: string
  address: string
  role?: AgentTeamMemberRole
  template_id?: number
  template_version_id?: number
  capabilities?: Record<string, unknown>
}

export interface AgentTeamTaskInput {
  task_key: string
  member_key: string
  title: string
  instructions: string
  depends_on?: string[]
  input?: Record<string, unknown>
  priority?: number
  max_attempts?: number
}

export interface CreateAgentTeamInput {
  surface: AgentTeamSurface
  session_id: string
  title: string
  objective: string
  members: AgentTeamMemberInput[]
  tasks: AgentTeamTaskInput[]
  max_active_children?: number
  max_attempts?: number
  priority?: number
  deadline_at?: string
  trace_id?: string
}

export interface AgentTeamMutationResult {
  team_id: number
  status: AgentTeamStatus
  summary?: Record<string, unknown>
  trace_id?: string
}

export function listAgentTeams(query?: AgentTeamListQuery): Promise<AgentTeamListResponse> {
  return get<AgentTeamListResponse>('/agent-teams', query)
}

export function getAgentTeam(teamId: number): Promise<AgentTeamDetail> {
  return get<AgentTeamDetail>(`/agent-teams/${encodeURIComponent(String(teamId))}`)
}

export function listAgentTeamMessages(
  teamId: number,
  beforeId: number,
  limit = 500,
): Promise<AgentTeamMessagePage> {
  return get<AgentTeamMessagePage>(`/agent-teams/${encodeURIComponent(String(teamId))}/messages`, {
    before_id: beforeId,
    limit,
  })
}

export function createAgentTeam(input: CreateAgentTeamInput): Promise<AgentTeamDetail> {
  return post<AgentTeamDetail>('/agent-teams', input)
}

export function cancelAgentTeam(teamId: number, reason = '用户取消'): Promise<AgentTeamMutationResult> {
  return post<AgentTeamMutationResult>(`/agent-teams/${encodeURIComponent(String(teamId))}/cancel`, { reason })
}

export function retryAgentTeam(
  teamId: number,
  task_keys: string[] = [],
  strategy_changes: Record<string, string> = {},
): Promise<AgentTeamMutationResult> {
  return post<AgentTeamMutationResult>(`/agent-teams/${encodeURIComponent(String(teamId))}/retry`, {
    task_keys,
    strategy_changes,
  })
}

export function archiveAgentTeam(teamId: number, reason = '归档'): Promise<AgentTeamMutationResult> {
  return post<AgentTeamMutationResult>(`/agent-teams/${encodeURIComponent(String(teamId))}/archive`, { reason })
}
