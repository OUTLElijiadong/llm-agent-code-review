/**
 * 安全告警 API 封装
 *
 * 对应后端:
 * - GET  /api/admin/observability/alerts/unread  拉取未读告警(require_admin)
 * - POST /api/admin/observability/alerts/{id}/read 标记告警已读(require_admin)
 */
import { get, post } from './http'
import type { SecurityAlert } from '@/types/securityAlert'

/** 拉取当前管理员未读的安全告警列表 */
export function fetchUnreadAlerts(): Promise<SecurityAlert[]> {
  return get<SecurityAlert[]>('/admin/observability/alerts/unread')
}

/** 将指定告警标记为已读 */
export function markAlertRead(id: number): Promise<unknown> {
  return post<unknown>(`/admin/observability/alerts/${id}/read`)
}
