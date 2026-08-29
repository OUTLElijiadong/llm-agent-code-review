/**
 * 审查类型常量定义
 * 统一后端 review_type 枚举到中文标签的映射,供任务列表、任务详情、报告详情等处复用,
 * 避免界面直接展示 quick/standard/full/discuss 等英文原始值。
 */

export type ReviewType =
  | 'quick'
  | 'standard'
  | 'security'
  | 'performance'
  | 'full'
  | 'discuss'

/**
 * review_type → 中文标签
 */
export const REVIEW_TYPE_LABELS: Record<string, string> = {
  quick: '快速审查',
  standard: '标准审查',
  security: '安全审查',
  performance: '性能审查',
  full: '全面审查',
  discuss: '圆桌讨论',
  sandbox_test: '沙箱黑白盒测试',
  pentest: '渗透测试',
}

/**
 * 获取审查类型的中文展示文案,未知类型回退为原值
 * @param type - 后端 review_type 原始值
 * @returns 用户可读的中文审查类型文案
 */
export function reviewTypeLabel(type?: string | null): string {
  if (!type) return '—'
  return REVIEW_TYPE_LABELS[type] ?? type
}
