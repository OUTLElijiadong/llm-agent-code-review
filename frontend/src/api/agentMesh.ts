import { get, post } from '@/api/http'

export type AgentMeshSurface = 'user' | 'admin'

export interface AgentMeshAddress {
  address: string
  name: string
  kind: 'runtime' | 'service' | 'custom' | 'session'
  status: string
  capabilities: string[]
  session_id: string
  surface: string
  last_seen_at: string
  description?: string
}

export interface AgentMeshMessage {
  schema_version: '1.0'
  message_id: string
  idempotency_key: string
  trace_id: string
  correlation_id: string
  causation_id: string
  sent_from: string
  send_to: string
  message_type: string
  priority: 'low' | 'normal' | 'high' | 'critical'
  subject: string
  status: string
  payload: Record<string, unknown>
  context: Record<string, unknown>
  artifacts: Array<Record<string, unknown>>
  errors: Array<Record<string, unknown>>
  requires_ack: boolean
  max_attempts: number
  attempt_count: number
  expires_at: string
  create_time: string
  update_time: string
}

export interface AgentMeshDiscovery {
  items: AgentMeshAddress[]
  total: number
  by_kind: Record<string, number>
}

export function heartbeatAgentMesh(input: {
  surface: AgentMeshSurface
  session_id: string
  title: string
  active_run_id?: string
  active_run_status?: string
}): Promise<AgentMeshAddress> {
  return post<AgentMeshAddress>('/agent-mesh/conversations/heartbeat', input)
}

export function listAgentMeshAgents(): Promise<AgentMeshDiscovery> {
  return get<AgentMeshDiscovery>('/agent-mesh/agents')
}

export function pullAgentMeshInbox(
  surface: AgentMeshSurface,
  sessionId: string,
  limit = 20,
): Promise<AgentMeshMessage[]> {
  return get<AgentMeshMessage[]>('/agent-mesh/inbox', {
    surface,
    session_id: sessionId,
    limit,
  })
}

export function getAgentMeshTrace(traceId: string): Promise<{
  trace_id: string
  messages: AgentMeshMessage[]
  total: number
}> {
  return get<{
    trace_id: string
    messages: AgentMeshMessage[]
    total: number
  }>(`/agent-mesh/traces/${encodeURIComponent(traceId)}`)
}
