import { get, post, put } from './http'
import type { Page } from '@/types/common'

export interface Feedback {
  id: number
  user_id: number
  feedback_type: string
  content: string
  contact?: string | null
  status: string
  admin_reply?: string | null
  handled_by?: number | null
  handled_at?: string | null
  create_time: string
  update_time: string
}

/** 提交反馈 */
export function createFeedback(data: {
  feedback_type?: string; content: string; contact?: string
}): Promise<{ id: number }> {
  return post<{ id: number }>('/feedback', data)
}

/** 反馈列表 (scope=mine|all) */
export function getFeedbackList(params?: Record<string, unknown>): Promise<Page<Feedback>> {
  return get<Page<Feedback>>('/feedback', params)
}

/** 反馈详情 */
export function getFeedback(id: number): Promise<Feedback> {
  return get<Feedback>(`/feedback/${id}`)
}

/** 管理员回复反馈 */
export function replyFeedback(id: number, data: {
  admin_reply?: string; status?: string
}): Promise<Feedback> {
  return put<Feedback>(`/feedback/${id}/reply`, data)
}

/** 反馈统计 (管理员) */
export function getFeedbackStats(): Promise<Record<string, number>> {
  return get<Record<string, number>>('/feedback/stats')
}
