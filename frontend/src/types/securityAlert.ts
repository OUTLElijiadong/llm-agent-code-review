/**
 * 安全告警类型定义
 *
 * 对应后端 GET /api/admin/observability/alerts/unread 返回的 Alert 对象,
 * 字段与后端 AgentAlertOut 保持对齐(含即将扩展的 category/source/read_at/fingerprint 等)。
 */

/** 告警严重级别(升序) */
export type SecuritySeverity = 'info' | 'warning' | 'high' | 'critical'

/** 告警状态 */
export type SecurityAlertStatus = 'open' | 'resolved'

/**
 * 安全告警
 *
 * detail_json 为后端返回的告警详情 JSON,可能是字符串也可能是已解析对象,
 * 统一经 parseDetail() 解析后再读取 suggestion/ip/country 等字段。
 */
export interface SecurityAlert {
  id: number
  /** 告警类型,如 brute_force / sql_injection / abnormal_login 等 */
  alert_type: string
  severity: SecuritySeverity | string
  status: SecurityAlertStatus | string
  title: string
  /** 告警详情 JSON(字符串或已解析对象),含 suggestion/ip/country 等 */
  detail_json?: string | Record<string, unknown> | null
  /** 告警类别(可选,后端新字段) */
  category?: string
  /** 告警来源,如来源 IP 或系统模块(可选,后端新字段) */
  source?: string
  /** 关联用户 ID(可选,后端新字段) */
  user_id?: number | null
  /** 已读时间(可选,后端新字段) */
  read_at?: string | null
  /** 告警指纹,用于跨实例去重(可选,后端新字段) */
  fingerprint?: string | null
  /** 创建时间(可选,后端新字段,ISO 字符串) */
  created_at?: string | null
}

/** parseDetail() 的返回结构 */
export interface ParsedAlertDetail {
  /** 解析后的详情对象;无法解析时为 null */
  detail: Record<string, unknown> | null
  /** 从详情中提取的处置建议;缺失时为空字符串 */
  suggestion: string
}

/**
 * 将告警详情 JSON 解析为对象并提取 suggestion 摘要
 *
 * 兼容两种后端形态:detail_json 本身已是对象,或为 JSON 字符串。
 *
 * @param detailJson - 后端返回的 detail_json 字段
 * @returns 解析后的详情对象与 suggestion 建议
 */
export function parseDetail(detailJson: SecurityAlert['detail_json']): ParsedAlertDetail {
  let detail: Record<string, unknown> | null = null
  if (typeof detailJson === 'string' && detailJson.trim()) {
    try {
      const parsed: unknown = JSON.parse(detailJson)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        detail = parsed as Record<string, unknown>
      }
    } catch {
      // 单条告警详情解析失败不影响其他告警,静默降级为无详情
    }
  } else if (detailJson && typeof detailJson === 'object' && !Array.isArray(detailJson)) {
    detail = detailJson
  }
  const rawSuggestion = detail?.suggestion
  const suggestion = typeof rawSuggestion === 'string' ? rawSuggestion : ''
  return { detail, suggestion }
}
