import { download, get } from '@/api/http'
import { streamResponses } from '@/utils/responsesStream'

import type {
  ResponseApprovalRequiredEvent,
  ResponseInputRequiredEvent,
  ResponseStreamEvent,
} from '@/types/responses'
import type { AgentMeshMessage } from '@/api/agentMesh'

export interface AgentResponseImageAsset {
  id: number
  mime: string
  sha256: string
}

export interface AgentResponseSessionMessage {
  role: 'user' | 'assistant'
  content: string
  image_assets?: AgentResponseImageAsset[]
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
export interface AgentResponseCancelResult {
  run_id: string
  status: 'completed' | 'cancelled' | 'failed' | 'incomplete'
  output_text: string
  error: string
}

export async function cancelAgentResponseRun(
  surface: 'user' | 'admin',
  sessionId: string,
  runId: string,
  reason?: string,
): Promise<AgentResponseCancelResult> {
  const outcome: { result: AgentResponseCancelResult | null; error: string } = { result: null, error: '' }
  const handle = streamResponses({
    action: 'cancel', surface, session_id: sessionId, run_id: runId,
    cancel_reason: reason?.trim() || '', messages: [],
  }, {
    onEvent(event) {
      if (event.type === 'error') outcome.error = event.error?.message || event.message || '停止请求未确认'
      if (event.type === 'auth_expired') outcome.error = '登录状态已失效，停止请求未确认'
      if (event.type !== 'response.completed' && event.type !== 'response.cancelled'
        && event.type !== 'response.failed' && event.type !== 'response.incomplete') return
      if (event.response.id !== runId) return
      const error = event.response.error
      outcome.result = {
        run_id: runId,
        status: event.type.slice('response.'.length) as AgentResponseCancelResult['status'],
        output_text: typeof event.response.output_text === 'string' ? event.response.output_text : '',
        error: error && typeof error === 'object' && 'message' in error && typeof error.message === 'string'
          ? error.message : '',
      }
    },
  })
  await handle.done
  if (outcome.error) throw new Error(outcome.error)
  if (!outcome.result) throw new Error('停止请求未确认：未收到本次运行的终态')
  return outcome.result
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

/** 图片只经当前登录态认证接口读取，不将资产地址或blob回传到模型。 */
export function fetchAgentResponseImage(assetId: number): Promise<Blob> {
  if (!Number.isSafeInteger(assetId) || assetId < 1) throw new Error('图片资产编号无效')
  return download(`/agent-responses/assets/${assetId}/image`, undefined, {
    // 旧版本曾返回 max-age；升级后也必须回源重新鉴权，不直接复用旧私密缓存。
    headers: { 'Cache-Control': 'no-cache', Pragma: 'no-cache' },
  })
}
