/**
 * SecuritySentinel API 封装 (v2.1)
 */
import { get, post } from './http'
import type {
  SecurityChecklistOut,
  SecurityDashboardSummaryOut,
  SecurityScanAllProjectsIn,
  SecurityScanFileIn,
  SecurityScanOut,
  SecurityScanProjectIn,
  SecurityScanTaskIn,
} from '@/types/security'

/** OWASP Top10 + 内置敏感信息正则规则清单 */
export function getSecurityChecklist(): Promise<SecurityChecklistOut> {
  return get<SecurityChecklistOut>('/security/checklist')
}

/** 单文件安全扫描 */
export function scanFile(payload: SecurityScanFileIn): Promise<SecurityScanOut> {
  return post<SecurityScanOut>('/security/scan-file', payload)
}

/** 任务级安全复审 */
export function scanTask(payload: SecurityScanTaskIn): Promise<SecurityScanOut> {
  return post<SecurityScanOut>('/security/scan-task', payload)
}

/** 项目级威胁建模 */
export function scanProject(payload: SecurityScanProjectIn): Promise<SecurityScanOut> {
  return post<SecurityScanOut>('/security/scan-project', payload)
}

/** 全量项目安全扫描 */
export function scanAllProjects(payload: SecurityScanAllProjectsIn): Promise<SecurityScanOut> {
  return post<SecurityScanOut>('/security/scan-all-projects', payload)
}

/** 查询已落库的 finding (等价于 scan-task) */
export function listFindings(taskId: number): Promise<SecurityScanOut> {
  return get<SecurityScanOut>('/security/findings', { task_id: taskId })
}

/** 工作台安全态势汇总 (v2.1.1) */
export function getSecurityDashboard(days = 30): Promise<SecurityDashboardSummaryOut> {
  return get<SecurityDashboardSummaryOut>('/security/dashboard-summary', { days })
}
