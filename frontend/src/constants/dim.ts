/**
 * 维度键归一化常量 (v2.0)
 *
 * 后端 issue_type 字段在历史/AI 返回中存在多种写法,统一在此映射,
 * 避免散落在 Dashboard / ReportDetail / TaskDetail 中各自维护。
 */
import { PRISM_DIM_COLORS } from '@/components/chart/prismTheme'

export const DIM_KEYS = [
  'style',
  'naming',
  'comment',
  'maintain',
  'performance',
  'exception',
  'logic',
  'security',
] as const

export type DimKey = typeof DIM_KEYS[number]

/**
 * 别名映射: 把后端各种写法折回标准 DimKey
 * - maintainability/maintain  → maintain
 * - bug/logic                 → logic
 * - 中文枚举(代码规范/潜在Bug...) → 对应 DimKey
 */
export const DIM_ALIAS: Record<string, DimKey> = {
  // 英文同义
  maintainability: 'maintain',
  bug: 'logic',
  perf: 'performance',
  // 中文枚举(与 ISSUE_TYPES 对齐)
  代码规范: 'style',
  命名规范: 'naming',
  注释完整性: 'comment',
  可维护性: 'maintain',
  性能问题: 'performance',
  异常处理: 'exception',
  潜在Bug: 'logic',
  安全漏洞: 'security',
}

export const DIM_LABELS: Record<DimKey, string> = {
  style: '代码规范',
  naming: '命名规范',
  comment: '注释完整性',
  maintain: '可维护性',
  performance: '性能问题',
  exception: '异常处理',
  logic: '潜在 Bug',
  security: '安全漏洞',
}

export const DIM_COLORS: Record<DimKey, string> = {
  style: PRISM_DIM_COLORS[0],
  naming: PRISM_DIM_COLORS[1],
  comment: PRISM_DIM_COLORS[2],
  maintain: PRISM_DIM_COLORS[3],
  performance: PRISM_DIM_COLORS[4],
  exception: PRISM_DIM_COLORS[5],
  logic: PRISM_DIM_COLORS[6],
  security: PRISM_DIM_COLORS[7],
}

/**
 * 归一化任意 issue_type 字符串为标准 DimKey
 *
 * @param raw - 后端返回的 issue_type
 * @returns 标准化的 DimKey,未识别返回 null
 */
export function normalizeDimKey(raw: string | null | undefined): DimKey | null {
  if (!raw) return null
  if ((DIM_KEYS as readonly string[]).includes(raw)) return raw as DimKey
  return DIM_ALIAS[raw] ?? null
}

/**
 * 用标签获取颜色,未识别返回灰色
 */
export function dimColor(raw: string | null | undefined): string {
  const k = normalizeDimKey(raw)
  return k ? DIM_COLORS[k] : '#9BA3B0'
}

/**
 * 用标签获取中文名,未识别返回原字符串
 */
export function dimLabel(raw: string | null | undefined): string {
  const k = normalizeDimKey(raw)
  return k ? DIM_LABELS[k] : (raw ?? '其他')
}

export const DIM_META: { key: DimKey; name: string; color: string }[] = DIM_KEYS.map((k) => ({
  key: k,
  name: DIM_LABELS[k],
  color: DIM_COLORS[k],
}))
