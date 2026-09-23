import { get, post } from '@/api/http'

export type AgentMeshSurface = 'user' | 'admin'

export interface AgentMeshAddress {
  address: string
  name: string
  kind: 'runtime' | 'service' | 'custom' | 'session'
  status: string
  dispatch_state?: string
  capabilities: string[]
  session_id: string
  surface: string
  last_seen_at: string
  active_run_id?: string
  active_run_status?: string
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
  last_error?: string
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

export type AgentMeshConversationStatus = 'active' | 'archived'

/** 当前登录账号的服务端会话摘要。 */
export interface AgentMeshConversation {
  session_id: string
  surface: AgentMeshSurface
  title: string
  status: AgentMeshConversationStatus
  last_seen_at: string
  active_run_id?: string
  active_run_status?: string
}

export interface AgentMeshConversationPage {
  items: AgentMeshConversation[]
  total: number
  limit: number
  offset: number
}

export function heartbeatAgentMesh(input: {
  surface: AgentMeshSurface
  session_id: string
  title: string
  active_run_id?: string
  active_run_status?: string
}, silentGone = false): Promise<AgentMeshAddress> {
  return post<AgentMeshAddress>(
    '/agent-mesh/conversations/heartbeat', input, undefined,
    silentGone ? [40921] : undefined,
  )
}

export function listAgentMeshAgents(surface?: AgentMeshSurface): Promise<AgentMeshDiscovery> {
  return get<AgentMeshDiscovery>('/agent-mesh/agents', surface ? { surface } : undefined)
}

/**
 * 从服务端读取当前账号的会话目录。账号范围由后端从登录态确定，
 * 前端不传 user_id，防止跨账号查询。
 */
export function listAgentMeshConversations(input: {
  surface: AgentMeshSurface
  status: AgentMeshConversationStatus
  query?: string
  limit?: number
  offset?: number
}): Promise<AgentMeshConversationPage> {
  return get<AgentMeshConversationPage>('/agent-mesh/conversations', input)
}

/** 恢复当前账号已归档的会话。 */
export function restoreAgentMeshConversation(
  surface: AgentMeshSurface,
  sessionId: string,
): Promise<AgentMeshConversation> {
  return post<AgentMeshConversation>('/agent-mesh/conversations/restore', {
    surface,
    session_id: sessionId,
  })
}

/**
 * 目标会话已归档/未注册(或表面归属不符)时的后端业务码。
 * 轮询收件箱命中它是正常生命周期(空会话 24h 被服务端定时归档、他端删除等),
 * 不该弹全局红字;由桥接层静默并触发会话收敛。
 */
export const AGENT_MESH_SESSION_GONE_CODE = 40321

export function pullAgentMeshInbox(
  surface: AgentMeshSurface,
  sessionId: string,
  limit = 20,
): Promise<AgentMeshMessage[]> {
  return post<AgentMeshMessage[]>('/agent-mesh/inbox/pull', undefined, {
    surface,
    session_id: sessionId,
    limit,
  }, [AGENT_MESH_SESSION_GONE_CODE])
}

/**
 * 对不需要自动执行的结构化消息写入完成回执。
 * 例如后台 JARVIS 简报在成本保护模式下只进入告警记录,不启动模型回合。
 */
export function acknowledgeAgentMeshMessage(
  surface: AgentMeshSurface,
  sessionId: string,
  messageId: string,
  status: 'acknowledged' | 'processing' | 'completed' | 'failed' = 'completed',
  summary = '',
): Promise<{ message_id: string; status: string }> {
  return post<{ message_id: string; status: string }>(
    `/agent-mesh/messages/${encodeURIComponent(messageId)}/ack`,
    { status, summary },
    { surface, session_id: sessionId },
  )
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

/**
 * 归档当前账户的一个小菱会话,使其从服务端发现目录隐藏。
 * 运行中/等待审批/等待输入的会话会被服务端拒绝。
 */
export function archiveAgentMeshSession(
  surface: AgentMeshSurface,
  sessionId: string,
): Promise<{ session_id: string; status: string }> {
  return post<{ session_id: string; status: string }>('/agent-mesh/conversations/archive', {
    surface,
    session_id: sessionId,
  })
}
