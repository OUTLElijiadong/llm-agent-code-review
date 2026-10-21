/**
 * SecuritySentinel API 封装 (v2.1)
 */
import { get, post } from './http'
import type {
  SecurityChecklistOut,
  SecurityRuleCatalogOut,
  SecurityDashboardSummaryOut,
  SecurityScanAllProjectsIn,
  SecurityScanFileIn,
  SecurityScanOut,
  SecurityScanProjectIn,
  SecurityScanTaskIn,
} from '@/types/security'

export type VulnerabilitySourceCode = 'cnvd' | 'cnnvd'
export type VulnerabilitySourceState =
  | 'not_checked'
  | 'available'
  | 'empty'
  | 'blocked'
  | 'rate_limited'
  | 'timeout'
  | 'unavailable'
  | 'invalid_response'
  | 'stale'

export interface VulnerabilitySourceEvidenceOut {
  url: string
  method: string
  checked_at: string
  http_status: number | null
  upstream_code: string | null
  response_sha256: string
  response_bytes: number
  cached: boolean
  retry_after_seconds: number | null
}

export interface VulnerabilitySourceStatusOut {
  code: VulnerabilitySourceCode
  name: string
  role: string
  url: string
  integration: 'reference' | 'implemented'
  adapter: string | null
  status: VulnerabilitySourceState | null
  connected: boolean | null
  status_message: string
  checked_at: string | null
  last_success_at: string | null
  http_status: number | null
  upstream_code: string | null
  record_count: number
  cached: boolean
  query_scope: string
  coverage_scope: 'ai_sector' | null
  coverage_label: string
  primary_status: string | null
  primary_http_status: number | null
  last_query: string
  retry_after_seconds: number | null
  evidence: VulnerabilitySourceEvidenceOut[]
}

export interface VulnerabilityRecordOut {
  source: VulnerabilitySourceCode
  record_id: string
  title: string
  url: string
  description: string
  coverage_scope: 'ai_sector' | null
  source_date: string | null
  severity: string
  cve_ids: string[]
  cwe_ids: string[]
  published_at: string | null
  updated_at: string | null
  checked_at: string
  match_type: 'latest' | 'keyword' | 'exact_identifier'
  evidence_url: string
  evidence_sha256: string
}

export interface VulnerabilitySourceQueryOut {
  query: string
  sources: VulnerabilitySourceStatusOut[]
  records: VulnerabilityRecordOut[]
}

export interface VulnerabilitySourceQueryIn {
  q?: string
  source?: 'all' | VulnerabilitySourceCode
  limit?: number
}

/** 主动读取 CNVD/CNNVD 官方来源连接状态。 */
export function getVulnerabilitySources(): Promise<VulnerabilitySourceStatusOut[]> {
  return get<VulnerabilitySourceStatusOut[]>('/security/vulnerability-sources')
}

/** 查询国内官方漏洞源；平台成功响应不代表来源连接成功，需检查 sources。 */
export function getVulnerabilityRecords(
  params: VulnerabilitySourceQueryIn = {},
): Promise<VulnerabilitySourceQueryOut> {
  return get<VulnerabilitySourceQueryOut>('/security/vulnerability-records', params)
}

/** OWASP Top10 + 内置敏感信息正则规则清单 */
export function getSecurityChecklist(): Promise<SecurityChecklistOut> {
  return get<SecurityChecklistOut>('/security/checklist')
}

/** 统一规则事实目录与执行器状态 */
export function getSecurityRuleCatalog(): Promise<SecurityRuleCatalogOut> {
  return get<SecurityRuleCatalogOut>('/security/rule-catalog')
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
