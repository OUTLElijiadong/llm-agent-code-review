import { get } from '@/api/http'

import type {
  ResponseApprovalRequiredEvent,
  ResponseInputRequiredEvent,
  ResponseStreamEvent,
} from '@/types/responses'
import type { AgentMeshMessage } from '@/api/agentMesh'

export interface AgentResponseSessionMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface AgentResponseSessionRun {
  run_id: string
  status: string
  model: string
  rounds: number
  error: string
  output_text?: string
  mesh_message_id?: string
  updated_at: string
}

export interface AgentResponseSession {
  surface: 'user' | 'admin'
  session_id: string
  run: AgentResponseSessionRun | null
  messages: AgentResponseSessionMessage[]
  events?: ResponseStreamEvent[]
  last_sequence_number?: number
  pending: ResponseApprovalRequiredEvent | ResponseInputRequiredEvent | null
  mesh_messages?: AgentMeshMessage[]
}

/**
 * 恢复当前用户在指定 Responses 界面的最近会话。
 */
export function getAgentResponseSession(
  surface: 'user' | 'admin',
  sessionId: string,
): Promise<AgentResponseSession> {
  return get<AgentResponseSession>('/agent-responses/session', {
    surface,
    session_id: sessionId,
  })
}
