/**
 * 标准 cron 五段表达式(分 时 日 月 周)前端校验。
 *
 * 用途:治理工作台调度任务的计划输入即时校验,防止非法 cron 静默入库
 * 导致任务停摆。最终裁决仍在后端,这里做格式层防错(尼尔森·防错原则)。
 */

const CRON_FIELD_LIMITS = [
  { min: 0, max: 59 }, // 分
  { min: 0, max: 23 }, // 时
  { min: 1, max: 31 }, // 日
  { min: 1, max: 12 }, // 月
  { min: 0, max: 6 }, // 星期 (APScheduler: 周一=0)
] as const

const MONTH_ALIASES = Object.fromEntries(
  ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
    .map((name, index) => [name, index + 1]),
)
const WEEKDAY_ALIASES = Object.fromEntries(
  ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
    .map((name, index) => [name, index]),
)

function validCronField(
  field: string,
  min: number,
  max: number,
  aliases: Record<string, number> = {},
): boolean {
  if (!field) return false
  return field.split(',').every((part) => {
    if (!part) return false
    const [rangePart, stepPart, extra] = part.split('/')
    if (extra !== undefined) return false
    if (stepPart !== undefined && (!/^\d+$/.test(stepPart) || Number(stepPart) < 1)) return false
    if (rangePart === '*') return true

    const range = /^([a-z]+|\d+)(?:-([a-z]+|\d+))?$/i.exec(rangePart)
    if (!range) return false
    const parseValue = (value: string): number => {
      if (/^\d+$/.test(value)) return Number(value)
      return aliases[value.toLowerCase()] ?? Number.NaN
    }
    const start = parseValue(range[1]!)
    const end = range[2] === undefined ? start : parseValue(range[2])
    return start >= min && start <= max && end >= min && end <= max && start <= end
  })
}

/**
 * 校验 cron 表达式是否为合法五段格式。
 * @param expr 原始输入
 * @returns true=格式合法(语义如 2月30日 交由后端裁决)
 */
export function isCronValid(expr: string): boolean {
  const value = expr.trim()
  if (!value) return false
  const parts = value.split(/\s+/)
  if (parts.length !== CRON_FIELD_LIMITS.length) return false
  return parts.every((part, index) => {
    const limits = CRON_FIELD_LIMITS[index]
    const aliases = index === 3 ? MONTH_ALIASES : index === 4 ? WEEKDAY_ALIASES : undefined
    return Boolean(limits && validCronField(part, limits.min, limits.max, aliases))
  })
}

export function isScheduleValid(expr: string): boolean {
  const value = expr.trim()
  if (!value || value === 'manual') return value === 'manual'

  const daily = /^daily@(\d{1,2}):(\d{2})$/.exec(value)
  if (daily) return Number(daily[1]) <= 23 && Number(daily[2]) <= 59

  const hourly = /^hourly@(?:\*:)?(\d{1,2})$/.exec(value)
  if (hourly) return Number(hourly[1]) <= 59

  const minutes = /^interval@(\d+)[mM]$/.exec(value)
  if (minutes) return Number(minutes[1]) >= 1 && Number(minutes[1]) <= 1440

  const seconds = /^interval@(\d+)[sS]$/.exec(value)
  if (seconds) return Number(seconds[1]) >= 1 && Number(seconds[1]) <= 86400

  return isCronValid(value)
}
