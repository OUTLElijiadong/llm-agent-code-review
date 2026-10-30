/**
 * 格式化工具函数
 * 提供时间格式化、文件大小格式化等通用工具方法
 *
 * 时区约定: 服务端时间一律解释为 UTC(带 Z、带偏移、不带偏移的历史格式、空格分隔均可),
 * 展示一律为北京时间(Asia/Shanghai),不依赖用户电脑时区。
 * 唯一实现位于 {@link ./serverTime}；本文件的时间函数只是保持旧导入路径可用的薄封装,
 * 新代码请直接使用 `parseServerTime` / `formatServerTime`。
 */

import {
  formatServerTime,
  formatServerTimeRelative,
  formatServerTimeWithTemplate,
  serverTimeMs,
  type ServerTimeStyle,
} from '@/utils/serverTime'

/** 历史上 formatDateTime 调用点用到的固定模板，映射到统一样式。 */
const TEMPLATE_STYLES: Record<string, ServerTimeStyle> = {
  'YYYY-MM-DD HH:mm:ss': 'datetime',
  'YYYY-MM-DD HH:mm': 'minute',
  'YYYY-MM-DD': 'date',
  'HH:mm:ss': 'time',
  'MM-DD HH:mm': 'short',
  'HH:mm': 'hm',
}

/**
 * 解析服务端时间为 UTC 毫秒时间戳；无法解析返回 NaN。
 * 兼容团队旧接口省略时区后缀的 ISO 字符串。
 */
export function parseUtcTimestamp(value: string | undefined | null): number {
  return serverTimeMs(value)
}

/**
 * 格式化日期时间为北京时间字符串
 * @param date - 服务端时间字符串、毫秒时间戳或 Date 对象
 * @param template - 展示模板，默认 "YYYY-MM-DD HH:mm:ss"；仅支持历史上用到的几种固定模板
 * @returns 北京时间字符串；空值返回 "-"
 */
export function formatDateTime(
  date: string | number | Date | undefined | null,
  template: string = 'YYYY-MM-DD HH:mm:ss',
): string {
  if (!date) return '-'
  const style = TEMPLATE_STYLES[template]
  if (style) return formatServerTime(date, style)
  // 少见的自定义模板：先换算到北京时间再套模板，仍然不依赖进程时区。
  return formatServerTimeWithTemplate(date, template)
}

/**
 * 格式化为北京时间日期（YYYY-MM-DD）
 * @param date - 服务端时间字符串、毫秒时间戳或 Date 对象
 * @returns 北京时间日期字符串；空值返回 "-"
 */
export function formatDate(date: string | number | Date | undefined | null): string {
  if (!date) return '-'
  return formatServerTime(date, 'date')
}

/**
 * 格式化为相对时间字符串（如 "3 小时前"），与绝对时间共用同一套解析
 * @param date - 服务端时间字符串、毫秒时间戳或 Date 对象
 * @returns 相对时间描述字符串
 */
export function formatRelativeTime(date: string | number | Date | undefined | null): string {
  if (!date) return '-'
  return formatServerTimeRelative(date)
}

/**
 * 格式化文件大小为可读字符串
 * @param bytes - 文件字节数
 * @param decimals - 保留小数位数，默认为2
 * @returns 带单位的文件大小字符串（如 "1.50 MB"）
 */
export function formatFileSize(bytes: number | undefined | null, decimals: number = 2): string {
  if (bytes === undefined || bytes === null || bytes < 0) return '0 B'
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  const size = parseFloat((bytes / Math.pow(k, i)).toFixed(decimals))
  return `${size} ${sizes[i]}`
}

/**
 * 格式化毫秒数为可读时长字符串
 * @param ms - 毫秒数
 * @returns 时长字符串（如 "2分30秒" 或 "1小时15分"）
 */
export function formatDuration(ms: number | undefined | null): string {
  if (!ms || ms < 0) return '-'
  const seconds = Math.floor(ms / 1000)
  if (seconds < 60) return `${seconds}秒`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) {
    const remainSeconds = seconds % 60
    return remainSeconds > 0 ? `${minutes}分${remainSeconds}秒` : `${minutes}分钟`
  }
  const hours = Math.floor(minutes / 60)
  const remainMinutes = minutes % 60
  return remainMinutes > 0 ? `${hours}小时${remainMinutes}分` : `${hours}小时`
}

/**
 * 格式化数字为千分位分隔字符串
 * @param num - 需要格式化的数字
 * @returns 千分位分隔后的字符串（如 "1,234,567"）
 */
export function formatNumber(num: number | undefined | null): string {
  if (num === undefined || num === null) return '-'
  return num.toLocaleString('zh-CN')
}

/**
 * 格式化百分比
 * @param value - 0-1之间的数值
 * @param decimals - 保留小数位数，默认为1
 * @returns 百分比字符串（如 "85.5%"）
 */
export function formatPercent(value: number | undefined | null, decimals: number = 1): string {
  if (value === undefined || value === null) return '-'
  return `${(value * 100).toFixed(decimals)}%`
}
