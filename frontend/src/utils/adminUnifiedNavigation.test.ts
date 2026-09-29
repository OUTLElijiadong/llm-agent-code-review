import { describe, expect, it } from 'vitest'
import { resolveAdminSection, sectionQuery } from './adminUnifiedNavigation'

describe('统一管理中心导航', () => {
  it.each([
    ['agents', 'releases', 'approvals'],
    ['agents', 'evolution', 'approvals'],
    ['operations', 'ai-logs', 'logs'],
    ['operations', 'audit', 'logs'],
  ] as const)('保留历史入口 %s/%s 并映射到 %s', (domain, legacySection, expected) => {
    expect(resolveAdminSection(domain, legacySection)).toBe(expected)
  })

  it('不改变其他分区的原始入口，并在切换时保留过滤参数', () => {
    expect(resolveAdminSection('access', 'roles')).toBe('roles')
    expect(sectionQuery({ keyword: '登录', logType: 'ai' }, 'logs')).toEqual({
      keyword: '登录', logType: 'ai', section: 'logs',
    })
  })
})
