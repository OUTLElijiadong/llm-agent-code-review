import dayjs from 'dayjs'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  BEIJING_LABEL,
  SERVER_TIME_ZONE,
  formatServerTime,
  formatServerTimeRelative,
  parseServerTime,
  serverTimeMs,
} from '@/utils/serverTime'

/**
 * 同一个任务 #231：库内时间 08:20（UTC），北京时间应显示 16:20。
 * 本文件在 TZ=UTC / America/Los_Angeles / Asia/Shanghai 三种进程时区下各跑一遍，结果必须一致
 * （见 package.json 的 test:time-zones 脚本）。
 */
const STORED_MS = Date.UTC(2026, 9, 22, 8, 20, 0)

const EVERY_HISTORICAL_SHAPE = [
  '2026-10-22T08:20:00',
  '2026-10-22T08:20:00Z',
  '2026-10-22T08:20:00z',
  '2026-10-22T08:20:00+00:00',
  '2026-10-22T08:20:00+0000',
  '2026-10-22 08:20:00',
  '2026-10-22 08:20:00Z',
  '2026-10-22T16:20:00+08:00',
  '2026-10-22T08:20:00.000000',
  '2026-10-22T08:20:00.000000Z',
  '2026-10-22T08:20:00.123456+00:00',
  '2026-10-22 08:20',
  '  2026-10-22T08:20:00Z  ',
] as const

afterEach(() => {
  vi.useRealTimers()
})

describe('parseServerTime', () => {
  it.each(EVERY_HISTORICAL_SHAPE)('把 %j 解释为同一个 UTC 时刻', (value) => {
    const parsed = parseServerTime(value)
    expect(parsed).not.toBeNull()
    // 毫秒小数允许存在，截断到秒后必须相同。
    expect(Math.floor((parsed as number) / 1000)).toBe(STORED_MS / 1000)
  })

  it('接受 Date 与毫秒时间戳', () => {
    expect(parseServerTime(new Date(STORED_MS))).toBe(STORED_MS)
    expect(parseServerTime(STORED_MS)).toBe(STORED_MS)
  })

  it('空值与无法解析的文本返回 null，不抛错', () => {
    for (const value of [null, undefined, '', '   ', '昨天', 'not-a-time', '2026-13-45T99:00:00']) {
      expect(parseServerTime(value as never)).toBeNull()
    }
    expect(parseServerTime(Number.NaN)).toBeNull()
    expect(parseServerTime(new Date('invalid'))).toBeNull()
  })

  it('已带 Z 的值不会被再补一次 Z（不会出现 ZZ 之类的无效串）', () => {
    expect(parseServerTime('2026-10-22T08:20:00Z')).toBe(STORED_MS)
    expect(parseServerTime('2026-10-22T08:20:00ZZ')).toBeNull()
  })

  it('serverTimeMs 无法解析时返回 NaN，便于直接参与排序和相减', () => {
    expect(serverTimeMs('2026-10-22T08:20:00')).toBe(STORED_MS)
    expect(Number.isNaN(serverTimeMs(undefined))).toBe(true)
  })
})

describe('formatServerTime', () => {
  it('固定使用北京时间，不依赖用户电脑时区', () => {
    expect(SERVER_TIME_ZONE).toBe('Asia/Shanghai')
    expect(BEIJING_LABEL).toBe('（UTC+8）')
  })

  it.each(EVERY_HISTORICAL_SHAPE)('把 %j 显示为北京时间 16:20', (value) => {
    expect(formatServerTime(value, 'minute')).toBe('2026-10-22 16:20')
    expect(formatServerTime(value, 'datetime')).toMatch(/^2026-10-22 16:20:00$/)
  })

  it('各展示样式', () => {
    const value = '2026-10-22T08:20:05Z'
    expect(formatServerTime(value, 'datetime')).toBe('2026-10-22 16:20:05')
    expect(formatServerTime(value, 'minute')).toBe('2026-10-22 16:20')
    expect(formatServerTime(value, 'date')).toBe('2026-10-22')
    expect(formatServerTime(value, 'time')).toBe('16:20:05')
    expect(formatServerTime(value, 'short')).toBe('10-22 16:20')
    expect(formatServerTime(value, 'hm')).toBe('16:20')
    expect(formatServerTime(value, 'datetime-label')).toBe('2026-10-22 16:20:05（UTC+8）')
    expect(formatServerTime(value)).toBe('2026-10-22 16:20')
  })

  it('跨日与跨年边界按北京时间换算', () => {
    expect(formatServerTime('2026-10-22T20:30:00Z', 'minute')).toBe('2026-10-23 04:30')
    expect(formatServerTime('2026-12-31T16:00:00Z', 'date')).toBe('2027-01-01')
    expect(formatServerTime('2026-10-22T15:59:59', 'datetime')).toBe('2026-10-22 23:59:59')
  })

  it('空值显示占位符，可自定义；无法解析的文本原样返回以免丢信息', () => {
    expect(formatServerTime(null)).toBe('-')
    expect(formatServerTime(undefined, 'minute')).toBe('-')
    expect(formatServerTime('', 'minute', { fallback: '暂未审查' })).toBe('暂未审查')
    expect(formatServerTime('未记录', 'minute')).toBe('未记录')
  })

  it('不使用夏令时：北京时间全年固定 UTC+8', () => {
    expect(formatServerTime('2026-01-15T00:00:00Z', 'hm')).toBe('08:00')
    expect(formatServerTime('2026-07-15T00:00:00Z', 'hm')).toBe('08:00')
  })

  it('接受毫秒时间戳与 Date', () => {
    expect(formatServerTime(STORED_MS, 'minute')).toBe('2026-10-22 16:20')
    expect(formatServerTime(new Date(STORED_MS), 'minute')).toBe('2026-10-22 16:20')
  })
})

describe('formatServerTimeRelative', () => {
  it('相对时间与绝对时间走同一套解析：不带 Z 的库内值也按 UTC 计算', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(Date.UTC(2026, 9, 22, 9, 20, 0)))
    // 当前 09:20 UTC，库内 08:20 UTC，应是 1 小时前，而不是 9 小时前或 -7 小时。
    expect(formatServerTimeRelative('2026-10-22T08:20:00')).toBe('1 小时前')
    expect(formatServerTimeRelative('2026-10-22T08:20:00Z')).toBe('1 小时前')
    expect(formatServerTimeRelative('2026-10-22 08:20:00')).toBe('1 小时前')
  })

  it('刚刚、分钟、天，以及空值与异常值', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(Date.UTC(2026, 9, 22, 8, 20, 30)))
    expect(formatServerTimeRelative('2026-10-22T08:20:00')).toBe('刚刚')
    expect(formatServerTimeRelative('2026-10-22T08:05:00')).toBe('15 分钟前')
    expect(formatServerTimeRelative('2026-10-20T08:20:00')).toBe('2 天前')
    expect(formatServerTimeRelative(null)).toBe('-')
    expect(formatServerTimeRelative('乱码')).toBe('乱码')
  })

  it('超过 30 天回落到北京时间的绝对日期', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(Date.UTC(2026, 11, 22, 8, 20, 0)))
    expect(formatServerTimeRelative('2026-10-22T08:20:00')).toBe('2026-10-22 16:20')
  })

  it('未来时间（时钟偏差）不显示负数', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(Date.UTC(2026, 9, 22, 8, 19, 0)))
    expect(formatServerTimeRelative('2026-10-22T08:20:00')).toBe('刚刚')
  })
})

describe('进程时区无关性', () => {
  it('当前进程时区下，格式化结果与固定期望一致', () => {
    // package.json 的 test:time-zones 在三种 TZ 下运行本文件；这里把进程实际偏移和 TZ 变量对上，
    // 防止 worker 没有继承 TZ 导致三次运行其实都在同一个时区。
    const expectedOffsetByZone: Record<string, number> = {
      UTC: 0,
      'America/Los_Angeles': 420, // 2026-10-22 仍在夏令时（PDT, UTC-7）
      'Asia/Shanghai': -480,
    }
    const zone = process.env.TZ
    if (zone && zone in expectedOffsetByZone) {
      expect(new Date(2026, 9, 22).getTimezoneOffset()).toBe(expectedOffsetByZone[zone])
    }
    expect(formatServerTime('2026-10-22T08:20:00', 'minute')).toBe('2026-10-22 16:20')
    expect(formatServerTime('2026-10-22T08:20:00Z', 'minute')).toBe('2026-10-22 16:20')
  })

  it('遗留写法 dayjs(str) 在不同进程时区下会显示不同的钟点，这正是需要统一入口的原因', () => {
    // 这条只描述事实，不做断言分叉：新入口的结果与进程时区无关，旧写法的结果取决于输入是否带 Z。
    const naive = dayjs('2026-10-22T08:20:00').format('HH:mm') // 当作本地时间，永远 08:20
    const tagged = dayjs('2026-10-22T08:20:00Z').format('HH:mm') // 随进程时区变化
    expect(naive).toBe('08:20')
    const offsetHours = -new Date(2026, 9, 22).getTimezoneOffset() / 60
    expect(Number(tagged.slice(0, 2))).toBe((8 + offsetHours + 24) % 24)
    expect(formatServerTime('2026-10-22T08:20:00', 'hm')).toBe('16:20')
    expect(formatServerTime('2026-10-22T08:20:00Z', 'hm')).toBe('16:20')
  })
})
