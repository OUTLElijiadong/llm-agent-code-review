import { get } from '@/api/http'
import { getToken } from '@/utils/token'

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
  cancel_reason?: string
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
 * 取消当前运行：直接调用流端点上的 cancel 动作，后端会收敛检查点，
 * 不依赖本页面继续读取 SSE。
 */
export async function cancelAgentResponseRun(
  surface: 'user' | 'admin',
  sessionId: string,
  runId: string,
  reason?: string,
): Promise<void> {
  const response = await fetch('/api/agent-responses/stream', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${getToken()}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      action: 'cancel',
      surface,
      session_id: sessionId,
      run_id: runId,
      cancel_reason: reason?.trim() || '',
      messages: [],
    }),
  })
  if (!response.ok) throw new Error('取消任务请求失败')
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
