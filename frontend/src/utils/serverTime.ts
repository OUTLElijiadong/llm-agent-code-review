/**
 * 服务端时间的唯一解析与展示入口。
 *
 * 为什么需要它（审查任务 #231 事故）：
 * 库内时间不带时区、值是 UTC，旧接口输出 `2026-10-22T08:20:00`（无 Z）。各页面各写一套解析：
 * - `dayjs(str)` 把它当“本机本地时间”，所以报告列表/项目详情在任何时区都显示 08:20；
 * - `parseUtcTimestamp` 按 UTC 解析，却用本机时区格式化，所以任务列表/报告详情只有在中国大陆的电脑上是 16:20；
 * - 有的页面手工补 `Z`，新接口已带 `Z` 时会变成 `ZZ`。
 *
 * 约定：
 * 1. 输入：带 Z、带偏移、不带偏移（历史格式）、空格分隔的字符串，一律解释为 UTC 时刻；
 * 2. 输出：固定显示北京时间（Asia/Shanghai，全年 UTC+8），不依赖用户电脑时区；
 * 3. 相对时间（“3 分钟前”）与绝对时间走同一个 {@link parseServerTime}。
 *
 * 页面里不要再直接写 `dayjs(...)` / `new Date(...)` / `toLocale*String` 去处理服务端时间。
 */

import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import timezone from 'dayjs/plugin/timezone'
import utc from 'dayjs/plugin/utc'

dayjs.extend(utc)
dayjs.extend(timezone)
// 保持迁移前的全局状态：旧 format.ts 在加载时注册了中文 locale，这里沿用，避免任何隐式依赖行为漂移。
dayjs.locale('zh-cn')

/** 展示用的固定时区：北京时间，中国不使用夏令时。 */
export const SERVER_TIME_ZONE = 'Asia/Shanghai'
/** 导出文档、需要显式说明口径时附加的标注。 */
export const BEIJING_LABEL = '（UTC+8）'

/** 展示样式。 */
export type ServerTimeStyle =
  | 'datetime' // 2026-10-22 16:20:05
  | 'minute' // 2026-10-22 16:20（默认）
  | 'date' // 2026-10-22
  | 'time' // 16:20:05
  | 'short' // 10-22 16:20
  | 'hm' // 16:20
  | 'datetime-label' // 2026-10-22 16:20:05（UTC+8）

const STYLE_TEMPLATES: Record<Exclude<ServerTimeStyle, 'datetime-label'>, string> = {
  datetime: 'YYYY-MM-DD HH:mm:ss',
  minute: 'YYYY-MM-DD HH:mm',
  date: 'YYYY-MM-DD',
  time: 'HH:mm:ss',
  short: 'MM-DD HH:mm',
  hm: 'HH:mm',
}

export type ServerTimeInput = string | number | Date | null | undefined

export interface FormatServerTimeOptions {
  /** 空值或无法解析时的占位文本，默认 `-`。 */
  fallback?: string
}

// 日期 + 可选时间 + 可选小数秒 + 可选时区后缀。把 `Z` 之外的写法先归一，避免浏览器各自实现不一致。
const SERVER_TIME_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?)?\s*(Z|z|[+-]\d{2}(?::?\d{2})?)?$/

/**
 * 解析服务端时间，返回 UTC 毫秒时间戳；无法解析返回 null。
 *
 * 不带偏移的文本按 UTC 解释（库内 naive 值就是 UTC）。已带 Z/偏移的值按其偏移换算，且不会被重复补 Z。
 */
export function parseServerTime(value: ServerTimeInput): number | null {
  if (value === null || value === undefined) return null
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value.getTime()
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value !== 'string') return null

  const text = value.trim()
  if (!text) return null
  const match = SERVER_TIME_PATTERN.exec(text)
  if (!match) return null

  const [, year, month, day, hour = '0', minute = '0', second = '0', fraction = '', zone = ''] = match
  const millis = fraction ? Number(`0.${fraction}`) * 1000 : 0
  const base = Date.UTC(
    Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second),
  )
  // Date.UTC 会把 13 月、45 日之类“滚动”成合法日期；回读校验，拒绝这类无效输入。
  const probe = new Date(base)
  if (
    probe.getUTCFullYear() !== Number(year) || probe.getUTCMonth() !== Number(month) - 1
    || probe.getUTCDate() !== Number(day) || probe.getUTCHours() !== Number(hour)
    || probe.getUTCMinutes() !== Number(minute) || probe.getUTCSeconds() !== Number(second)
  ) {
    return null
  }
  return Math.round(base + millis - zoneOffsetMs(zone))
}

/** 解析服务端时间为毫秒时间戳；无法解析时返回 NaN，便于直接参与排序与相减。 */
export function serverTimeMs(value: ServerTimeInput): number {
  const parsed = parseServerTime(value)
  return parsed === null ? Number.NaN : parsed
}

function zoneOffsetMs(zone: string): number {
  if (!zone || zone === 'Z' || zone === 'z') return 0
  const sign = zone.startsWith('-') ? -1 : 1
  const digits = zone.slice(1).replace(':', '')
  const hours = Number(digits.slice(0, 2))
  const minutes = Number(digits.slice(2, 4) || '0')
  return sign * (hours * 60 + minutes) * 60_000
}

/**
 * 把服务端时间显示为北京时间。
 *
 * 无法解析的文本原样返回（宁可显示原文也不丢信息），空值返回占位符。
 */
export function formatServerTime(
  value: ServerTimeInput,
  style: ServerTimeStyle = 'minute',
  options: FormatServerTimeOptions = {},
): string {
  const fallback = options.fallback ?? '-'
  const parsed = parseServerTime(value)
  if (parsed === null) {
    if (value === null || value === undefined) return fallback
    if (typeof value === 'string') return value.trim() ? value : fallback
    return fallback
  }
  const moment = dayjs.utc(parsed).tz(SERVER_TIME_ZONE)
  if (style === 'datetime-label') return `${moment.format(STYLE_TEMPLATES.datetime)}${BEIJING_LABEL}`
  return moment.format(STYLE_TEMPLATES[style])
}

const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

/**
 * 按自定义 dayjs 模板显示北京时间。仅为旧调用点（如 'MM/DD HH:mm'）保留；
 * 新代码优先使用 {@link formatServerTime} 的固定样式。
 */
export function formatServerTimeWithTemplate(
  value: ServerTimeInput,
  template: string,
  options: FormatServerTimeOptions = {},
): string {
  const parsed = parseServerTime(value)
  if (parsed === null) return formatServerTime(value, 'minute', options)
  return dayjs.utc(parsed).tz(SERVER_TIME_ZONE).format(template)
}

/**
 * 相对时间（“3 分钟前”）。与绝对时间共用 {@link parseServerTime}，
 * 因此库内不带 Z 的值也按 UTC 计算，不会出现“8 小时前”或负数。
 * 超过 30 天回落到北京时间的绝对时刻。
 */
export function formatServerTimeRelative(
  value: ServerTimeInput,
  options: FormatServerTimeOptions = {},
): string {
  const parsed = parseServerTime(value)
  if (parsed === null) return formatServerTime(value, 'minute', options)
  const elapsed = Math.max(0, Date.now() - parsed)
  if (elapsed < MINUTE) return '刚刚'
  if (elapsed < HOUR) return `${Math.floor(elapsed / MINUTE)} 分钟前`
  if (elapsed < DAY) return `${Math.floor(elapsed / HOUR)} 小时前`
  if (elapsed < 30 * DAY) return `${Math.floor(elapsed / DAY)} 天前`
  return formatServerTime(parsed, 'minute', options)
}
