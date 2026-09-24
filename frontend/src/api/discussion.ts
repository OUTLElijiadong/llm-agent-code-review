import { get, post } from './http'
import type { DiscussionTurn } from '@/utils/discussionStream'

export interface StartDiscussionParams {
  project_id: number
  file_id: number
  review_type: string
}

export interface StartDiscussionResult {
  session_id: string
  ws_url: string
  file_name: string
  language: string
  review_type: string
  agents: Array<{ code: string; name: string }>
  rules_count: number
}

export function startDiscussion(params: StartDiscussionParams) {
  return post<StartDiscussionResult>('/discuss/start', undefined, params)
}

/** 服务端圆桌阶段。未知值由 UI 显示通用处理中状态。 */
export type DiscussionPhase =
  | 'pending' | 'speaking' | 'summarizing' | 'extracting' | 'reporting'
  | 'completed' | 'failed' | 'cancelled' | 'interrupted'

export interface DiscussionProgress {
  phase: DiscussionPhase | string
  completed_units: number
  total_units: number
  current_round: number
  speaker_code?: string
  seq: number
}

export interface DiscussionSessionSummary {
  session_id: string
  status: string
  file_name: string
  ws_url?: string
  agents?: Array<{ code: string; name: string }>
  max_rounds?: number
  report_task_id?: number
  progress?: DiscussionProgress | null
  /** Unix 秒；仅正常完成的圆桌有五分钟追问窗口。 */
  followup_until?: number
}

export interface DiscussionSessionDetail extends DiscussionSessionSummary {
  agents: Array<{ code: string; name: string }>
  turns?: DiscussionTurn[]
  has_earlier?: boolean
  next_before_seq?: number | null
}

export interface DiscussionSessionPage {
  items: DiscussionSessionSummary[]
  next_offset: number | null
}

/** 由服务端按登录账号筛选，客户端不缓存跨账号会话内容。 */
export function listDiscussionSessions(limit = 30, offset = 0) {
  return get<DiscussionSessionPage>('/discuss/sessions', { limit, offset }, undefined, true)
}

export function getDiscussionSession(sessionId: string, limit = 100, beforeSeq?: number) {
  return get<DiscussionSessionDetail>(`/discuss/sessions/${encodeURIComponent(sessionId)}`,
    { limit, ...(beforeSeq ? { before_seq: beforeSeq } : {}) })
}
