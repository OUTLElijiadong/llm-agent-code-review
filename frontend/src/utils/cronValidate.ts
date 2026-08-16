/**
 * 标准 cron 五段表达式(分 时 日 月 周)前端校验。
 *
 * 用途:治理工作台调度任务的计划输入即时校验,防止非法 cron 静默入库
 * 导致任务停摆。最终裁决仍在后端,这里做格式层防错(尼尔森·防错原则)。
 */

/** 单段:数字/星号,支持 范围(-)、步进(/)、列表(,) 组合。 */
const FIELD_SOURCE = String.raw`(\*|[0-9]|[1-5][0-9])(?:-[0-9]+)?(?:\/(?:[0-9]|[1-5][0-9]))?(?:,(?:[0-9]|[1-5][0-9])(?:-[0-9]+)?(?:\/(?:[0-9]|[1-5][0-9]))?)*`

const FIVE_FIELD_RE = new RegExp(`^${FIELD_SOURCE}(?: ${FIELD_SOURCE}){4}$`)

/**
 * 校验 cron 表达式是否为合法五段格式。
 * @param expr 原始输入
 * @returns true=格式合法(语义如 2月30日 交由后端裁决)
 */
export function isCronValid(expr: string): boolean {
  const value = expr.trim()
  if (!value) return false
  if (!FIVE_FIELD_RE.test(value)) return false
  const parts = value.split(/\s+/)
  const hour = parts[1]
  // 纯数字小时必须 ≤23(其余字段范围校验留给后端)
  if (/^[0-9]+$/.test(hour) && Number(hour) > 23) return false
  return true
}
