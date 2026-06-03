/**
 * 严重程度常量定义
 * 提供严重程度枚举值、颜色映射和Element Plus标签类型映射
 */

export const SEVERITY = ['严重', '高', '中', '低'] as const

export type Severity = (typeof SEVERITY)[number]
export type SeverityKey = 'severe' | 'high' | 'medium' | 'low'

export interface SeverityOption {
  value: Severity
  key: SeverityKey
  label: string
}

export const SEVERITY_OPTIONS: SeverityOption[] = [
  { value: '严重', key: 'severe', label: '危急' },
  { value: '高', key: 'high', label: '高' },
  { value: '中', key: 'medium', label: '中' },
  { value: '低', key: 'low', label: '低' },
]

const SEVERITY_ALIAS: Record<string, SeverityOption> = {
  严重: SEVERITY_OPTIONS[0],
  危急: SEVERITY_OPTIONS[0],
  severe: SEVERITY_OPTIONS[0],
  high: SEVERITY_OPTIONS[1],
  高: SEVERITY_OPTIONS[1],
  medium: SEVERITY_OPTIONS[2],
  中: SEVERITY_OPTIONS[2],
  low: SEVERITY_OPTIONS[3],
  低: SEVERITY_OPTIONS[3],
}

/**
 * 严重程度对应的显示颜色
 */
export const SEVERITY_COLOR: Record<Severity, string> = {
  严重: '#f56c6c',
  高: '#e6a23c',
  中: '#409eff',
  低: '#909399',
}

/**
 * 严重程度对应的Element Plus Tag组件type属性值
 */
export const SEVERITY_TAG_TYPE: Record<Severity, 'danger' | 'warning' | 'primary' | 'info'> = {
  严重: 'danger',
  高: 'warning',
  中: 'primary',
  低: 'info',
}

/**
 * 严重程度对应的得分权重
 */
export const SEVERITY_WEIGHT: Record<Severity, number> = {
  严重: 25,
  高: 15,
  中: 8,
  低: 3,
}

/**
 * 将后端中文严重度或旧版英文严重度统一转换为后端查询值
 * @param severity - 严重度原始值
 * @returns 后端使用的中文严重度
 */
export function normalizeSeverityValue(severity: string): Severity | string {
  return SEVERITY_ALIAS[severity]?.value ?? severity
}

/**
 * 将严重度转换为稳定 CSS class 后缀
 * @param severity - 严重度原始值
 * @returns CSS class 使用的严重度 key
 */
export function severityClass(severity: string): SeverityKey | string {
  return SEVERITY_ALIAS[severity]?.key ?? severity
}

/**
 * 获取严重度在界面上的显示文案
 * @param severity - 严重度原始值
 * @returns 用户可读的严重度文案
 */
export function severityDisplayLabel(severity: string): string {
  return SEVERITY_ALIAS[severity]?.label ?? severity
}
