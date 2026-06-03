import { get, del, download } from './http'
import type { Page } from '@/types/common'
import type { ReportListItem, ReportDetailOut } from '@/types/report'

export function getReports(params?: Record<string, unknown>) {
  return get<Page<ReportListItem>>('/reports', params)
}

export function getReportDetail(taskId: number) {
  return get<ReportDetailOut>(`/reports/${taskId}`)
}

export function exportWord(taskId: number) {
  return download(`/reports/${taskId}/export/word`)
}

export function exportPdf(taskId: number) {
  return download(`/reports/${taskId}/export/pdf`)
}

/**
 * 删除报告
 * @param taskId - 审查任务ID
 */
export function deleteReport(taskId: number): Promise<void> {
  return del<void>(`/reports/${taskId}`)
}
