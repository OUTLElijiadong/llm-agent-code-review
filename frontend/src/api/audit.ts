import { get } from './http'
import type { Page } from '@/types/common'
import type { AuditLogOut, AuditQuery } from '@/types/audit'

/**
 * 审计日志列表(需要 audit:view 权限)
 * @param params 查询条件
 * @returns 分页审计记录
 */
export function listAuditLogs(params: AuditQuery = {}): Promise<Page<AuditLogOut>> {
  return get<Page<AuditLogOut>>('/admin/audit', params as Record<string, unknown>)
}
