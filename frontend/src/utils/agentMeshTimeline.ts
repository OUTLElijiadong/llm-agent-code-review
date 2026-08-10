import type { AgentMeshMessage } from '@/api/agentMesh'
import type { ResponseToolCall, ResponseToolCallStatus } from '@/utils/responsesTimeline'

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
