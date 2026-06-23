import { get, post, put } from './http'
import type { Page } from '@/types/common'

export interface Ticket {
  id: number
  user_id: number
  title: string
  category: string
  description: string
  priority: string
  status: string
  admin_reply?: string | null
  handled_by?: number | null
  handled_at?: string | null
  create_time: string
  update_time: string
}

/** 提交维修工单 */
export function createTicket(data: {
  title: string; description: string; category?: string; priority?: string
}): Promise<{ id: number }> {
  return post<{ id: number }>('/maintenance', data)
}

/** 工单列表 (scope=mine|all) */
export function getTickets(params?: Record<string, unknown>): Promise<Page<Ticket>> {
  return get<Page<Ticket>>('/maintenance', params)
}

/** 工单详情 */
export function getTicket(id: number): Promise<Ticket> {
  return get<Ticket>(`/maintenance/${id}`)
}

/** 管理员受理/回复/改状态 */
export function handleTicket(id: number, data: {
  status?: string; admin_reply?: string; priority?: string
}): Promise<Ticket> {
  return put<Ticket>(`/maintenance/${id}/handle`, data)
}

/** 用户撤销/关闭自己的工单 */
export function closeTicket(id: number): Promise<Ticket> {
  return post<Ticket>(`/maintenance/${id}/close`)
}

/** 工单统计 (管理员) */
export function getTicketStats(): Promise<Record<string, number>> {
  return get<Record<string, number>>('/maintenance/stats')
}
