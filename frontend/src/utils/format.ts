/**
 * 格式化工具函数
 * 提供时间格式化、文件大小格式化等通用工具方法
 *
 * 时区约定: 后端返回 ISO 8601 UTC(+00:00) 格式,前端统一转换为本地时区显示。
 * dayjs 通过 utc 插件解析带时区偏移的字符串,format 输出自动转换为浏览器本地时区。
 */

import dayjs from 'dayjs'
import utc from 'dayjs/plugin/utc'
import timezone from 'dayjs/plugin/timezone'
import relativeTime from 'dayjs/plugin/relativeTime'
import 'dayjs/locale/zh-cn'

dayjs.extend(utc)
dayjs.extend(timezone)
dayjs.extend(relativeTime)
dayjs.locale('zh-cn')

/**
 * 格式化日期时间为标准字符串
 * @param date - 日期字符串、时间戳或Date对象
 * @param template - dayjs格式模板，默认为 "YYYY-MM-DD HH:mm:ss"
 * @returns 格式化后的日期时间字符串
 */
export function formatDateTime(
  date: string | number | Date | undefined | null,
  template: string = 'YYYY-MM-DD HH:mm:ss',
): string {
  if (!date) return '-'
  return dayjs(date).format(template)
}

/**
 * 格式化日期为简短字符串
 * @param date - 日期字符串、时间戳或Date对象
 * @returns 格式化后的日期字符串（YYYY-MM-DD）
 */
export function formatDate(date: string | number | Date | undefined | null): string {
  if (!date) return '-'
  return dayjs(date).format('YYYY-MM-DD')
}

/**
 * 格式化为相对时间字符串（如 "3小时前"）
 * @param date - 日期字符串、时间戳或Date对象
 * @returns 相对时间描述字符串
 */
export function formatRelativeTime(date: string | number | Date | undefined | null): string {
  if (!date) return '-'
  return dayjs(date).fromNow()
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
