/**
 * 报告模块类型定义
 *
 * 与后端 app/schemas/report.py、app/schemas/report_template.py 对齐,
 * 包含报告列表/详情、报告模板、报告生成请求体,以及 IssueOut v3 扩展字段。
 */

/** 报告模板类型(simple/detailed/compliance/custom,与后端 pattern 对齐) */
export type ReportTemplateType = 'simple' | 'detailed' | 'compliance' | 'custom'

/** 报告导出格式(json/html/pdf/word) */
export type ReportFormat = 'json' | 'html' | 'pdf' | 'word'

/** 合规映射结构(对应后端 IssueOut.compliance_mapping v3 字段) */
export interface ComplianceMapping {
  /** ISO 27001 控制点编号列表,如 ['A.9.4.2', 'A.12.6.1'] */
  iso27001: string[]
  /** GDPR 条款列表,如 ['Art.32', 'Art.5'] */
  gdpr: string[]
  /** PCI DSS 要求列表,如 ['Req.6.5.1', 'Req.8.2.1'] */
  pci_dss: string[]
  /** HIPAA 安全规则条款列表,如 ['164.312(a)(1)', '164.308(a)(3)'] */
  hipaa: string[]
}

/**
 * 报告中的问题项(扩展 T01 IssueOut v3 字段)。
 *
 * 在原 IssueOut 基础字段之上,新增:
 * - cvss_score: CVSS 3.x 基础评分(0-10)
 * - cvss_vector: CVSS 向量字符串(如 'AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H')
 * - compliance_mapping: 合规标准映射
 * - remediation: 详细修复方案(多行文本/markdown)
 * - static_rule_hits: 静态规则命中次数
 * - cwe: CWE 编号(如 'CWE-79')
 */
export interface ReportIssue {
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
  /** ===== T01 v3 新增字段 ===== */
  /** CVSS 3.x 基础评分(0-10,可选,无评分时为 undefined) */
  cvss_score?: number
  /** CVSS 向量字符串 */
  cvss_vector?: string
  /** 合规标准映射(ISO 27001 / GDPR / PCI DSS / HIPAA) */
  compliance_mapping?: ComplianceMapping
  /** 详细修复方案(多行文本,可包含 markdown) */
  remediation?: string
  /** 静态规则命中次数 */
  static_rule_hits?: number
  /** CWE 编号(如 'CWE-79'),由后端 issue_type 或单独字段提供 */
  cwe?: string
}

/** 报告列表项(与后端 ReportListItem 对齐) */
export interface ReportListItem {
  task_id: number
  task_name?: string
  project_name: string
  total_issues: number
  score: number
  status: 'success' | 'failed'
  create_time: string
}

/** 报告详情(与后端 ReportDetailOut 对齐,issues 需前端单独调用 issue API 获取) */
export interface ReportDetailOut {
  project: Record<string, unknown>
  task: Record<string, unknown>
  stats: Record<string, unknown>
  summary?: string
  files: Record<string, unknown>[]
  rules_snapshot: Record<string, unknown>[]
}

/**
 * 报告模板(与后端 ReportTemplateOut 对齐)。
 *
 * is_builtin 为 int 类型(0=自定义,1=内置),与后端 model 定义保持一致;
 * 内置模板不可删除,但可更新内容。
 */
export interface ReportTemplate {
  /** 模板主键 ID */
  id: number
  /** 模板名称(1-128 字符) */
  name: string
  /** 模板类型(simple/detailed/compliance/custom) */
  type: ReportTemplateType
  /** Jinja2 模板字符串 */
  content: string
  /** 是否内置(0=自定义,1=内置) */
  is_builtin: number
  /** 创建者用户 ID(内置模板可为 null) */
  creator_id?: number | null
  /** 模板描述(最长 255 字符) */
  description?: string | null
  /** 创建时间(ISO 8601 字符串) */
  create_time: string
  /** 更新时间(ISO 8601 字符串) */
  update_time: string
}

/** 报告模板创建请求体(与后端 ReportTemplateIn 对齐) */
export interface ReportTemplateCreateIn {
  /** 模板名称(1-128 字符) */
  name: string
  /** 模板类型(simple/detailed/compliance/custom) */
  type: ReportTemplateType
  /** Jinja2 模板字符串(非空) */
  content: string
  /** 模板描述(可选,最长 255 字符) */
  description?: string
}

/** 报告模板更新请求体(与后端 ReportTemplateUpdate 对齐,全部字段可选) */
export interface ReportTemplateUpdateIn {
  /** 模板名称 */
  name?: string
  /** 模板类型 */
  type?: ReportTemplateType
  /** Jinja2 模板字符串 */
  content?: string
  /** 模板描述 */
  description?: string
}

/** 报告生成请求体(与后端 ReportGenerateIn 对齐) */
export interface ReportGenerateIn {
  /** 审查任务 ID */
  task_id: number
  /** 导出格式(json/html/pdf/word) */
  format: ReportFormat
  /** 模板类型(simple/detailed/compliance,仅 html/pdf/word 使用) */
  template_type: ReportTemplateType
}
