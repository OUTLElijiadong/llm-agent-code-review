export interface RuleOut {
  id: number
  rule_code: string
  rule_name: string
  rule_type: string
  rule_content: string
  enabled: number
  is_builtin: number
  sort_order: number
}

/**
 * 合规映射结构
 * 4 个主流安全合规标准命中的条款列表
 */
export interface ComplianceMapping {
  iso27001: string[]
  gdpr: string[]
  pci_dss: string[]
  hipaa: string[]
}

export interface IssueOut {
  id: number
  task_id: number
  file_id?: number
  file_name?: string
  line_number?: number
  end_line?: number
  issue_type: string
  severity: string
  title?: string
  description: string
  suggestion?: string
  fixed_code?: string
  status: string
  create_time: string
  /** v3: 由有效 CVSS v3.1 向量确定性计算的评分(0-10) */
  cvss_score?: number | null
  /** v3: CVSS 向量字符串,如 "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H" */
  cvss_vector?: string | null
  /** v3: CVSS 版本；有效评分固定为 3.1 */
  cvss_version?: string | null
  /** v3: 评分来源；只有 vector 表示可验证的标准 CVSS */
  cvss_source?: string | null
  /** v3: 合规映射命中的标准条款 */
  compliance_mapping?: ComplianceMapping
  /** v3: 详细修复方案(markdown 文本) */
  remediation?: string
  /** v3: 静态规则引擎命中次数,大于 0 表示双引擎命中 */
  static_rule_hits?: number
  /** v3: 问题来源(LLM/静态/混合) */
  source?: string
  /** v3: 攻击场景说明 */
  exploit_scenario?: string
  /** v3: 漏洞证据代码片段 */
  evidence?: string
}

export interface IssueListItemOut extends IssueOut {
  project_id: number
  project_name: string
  task_name?: string
}

export interface IssueQuery {
  project_id?: number
  task_id?: number
  severity?: string
  issue_type?: string
  status?: string
  keyword?: string
  page?: number
  page_size?: number
}

export interface TaskOut {
  id: number
  task_name?: string
  project_id: number
  project_name: string
  review_type: string
  status: string
  total_files: number
  total_issues: number
  severe_issues: number
  high_issues: number
  medium_issues: number
  low_issues: number
  score: number
  duration_ms: number
  create_time: string
}

export interface TaskFileOut {
  file_id: number
  project_id: number
  file_name: string
  file_path?: string
  language: string
  line_count: number
  version_no: number
}

export interface TaskDetailOut {
  id: number
  task_name?: string
  project_id: number
  project_name: string
  review_type: string
  status: string
  total_files: number
  processed_files: number
  total_issues: number
  severe_issues: number
  high_issues: number
  medium_issues: number
  low_issues: number
  score: number
  summary?: string
  model_name?: string
  duration_ms: number
  start_time?: string
  end_time?: string
  create_time: string
  files: TaskFileOut[]
  agent_releases: Array<{
    release_id: number
    agent_code: string
    agent_name: string
    agent_version_id: number
    agent_version: number
    status: string
  }>
}

export interface ReviewStartIn {
  project_id: number
  file_ids: number[]
  review_type?: string
  task_name?: string
}

export interface RuleCreateIn {
  rule_code: string
  rule_name: string
  rule_type: string
  rule_content: string
  enabled?: number
  sort_order?: number
}

export interface RuleUpdateIn {
  rule_name?: string
  rule_type?: string
  rule_content?: string
  enabled?: number
  sort_order?: number
}

export interface IssueUpdateStatusIn {
  status: string
}

export interface IssueBatchUpdateStatusIn {
  ids: number[]
  status: string
}
