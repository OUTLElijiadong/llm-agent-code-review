import type { AgentMeshMessage } from '@/api/agentMesh'
import type { ResponseToolCall, ResponseToolCallStatus } from '@/utils/responsesTimeline'

export interface AgentMeshTimelineEntry {
  toolCalls?: ResponseToolCall[]
}

function statusOf(message: AgentMeshMessage): ResponseToolCallStatus {
  if (['failed', 'dead_letter', 'expired'].includes(message.status)) return 'failed'
  if (message.status === 'completed') return 'completed'
  return 'running'
}

export function agentMeshToolCalls(
  messages: AgentMeshMessage[] | undefined,
  currentAddress: string,
): ResponseToolCall[] {
  return (messages ?? []).map((message) => {
    const receiving = message.send_to === currentAddress
    const failed = ['failed', 'dead_letter', 'expired'].includes(message.status)
    return {
      key: `mesh-${message.message_id}`,
      callId: message.message_id,
      name: receiving ? 'receive_message' : 'send_message',
      agentCode: receiving ? message.sent_from : message.send_to,
      argumentsText: JSON.stringify({
        sent_from: message.sent_from,
        send_to: message.send_to,
        message_type: message.message_type,
        priority: message.priority,
        subject: message.subject,
        payload: message.payload,
      }),
      resultPreview: JSON.stringify({
        status: message.status,
        trace_id: message.trace_id,
        attempt_count: message.attempt_count,
      }),
      status: statusOf(message),
      error: failed ? `消息状态: ${message.status}` : undefined,
    }
  })
}

export function settleAgentMeshToolCalls(
  toolCalls: ResponseToolCall[] | undefined,
  succeeded: boolean,
  runStatus: string | null | undefined,
  error?: string | null,
): ResponseToolCall[] {
  return (toolCalls ?? []).map((toolCall, index) => {
    if (index > 0) return toolCall
    if (succeeded && runStatus === 'completed') {
      return { ...toolCall, status: 'completed', error: undefined }
    }
    if (runStatus === 'failed' || runStatus === 'cancelled') {
      return {
        ...toolCall,
        status: 'failed',
        error: error || (runStatus === 'cancelled' ? '响应已取消' : '响应执行失败'),
      }
    }
    return { ...toolCall, status: 'running' }
  })
}

export function findAgentMeshTimeline<T extends AgentMeshTimelineEntry>(
  entries: T[],
  messageId: string,
): T | undefined {
  return entries.find((entry) => entry.toolCalls?.some((toolCall) => toolCall.callId === messageId))
}

export function settleAgentMeshTimeline<T extends AgentMeshTimelineEntry>(
  entries: T[],
  messageId: string,
  succeeded: boolean,
  runStatus: string | null | undefined,
  error?: string | null,
): boolean {
  const timeline = findAgentMeshTimeline(entries, messageId)
  if (!timeline) return false
  timeline.toolCalls = settleAgentMeshToolCalls(timeline.toolCalls, succeeded, runStatus, error)
  return true
}
