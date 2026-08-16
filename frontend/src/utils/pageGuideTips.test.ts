import { describe, expect, it } from 'vitest'

import { ADMIN_PAGE_TIPS, USER_PAGE_TIPS, findPageGuideTip } from './pageGuideTips'

describe('页面主动引导目录', () => {
  it('精确路由优先匹配', () => {
    expect(findPageGuideTip('user', '/projects')?.title).toBe('项目管理')
    expect(findPageGuideTip('admin', '/admin/approvals')?.title).toBe('审批中心')
  })

  it('动态详情页按最长前缀匹配', () => {
    expect(findPageGuideTip('user', '/reviews/123')?.route).toBe('/reviews')
    expect(findPageGuideTip('admin', '/admin/observability/detail')?.route).toBe('/admin/observability')
  })

  it('无匹配路由不返回建议', () => {
    expect(findPageGuideTip('user', '/login')).toBeUndefined()
    expect(findPageGuideTip('admin', '/login')).toBeUndefined()
  })

  it('目录提示都带可执行预填指令', () => {
    for (const tip of [...USER_PAGE_TIPS, ...ADMIN_PAGE_TIPS]) {
      expect(tip.prompt.trim().length).toBeGreaterThan(0)
      expect(tip.hint.trim().length).toBeGreaterThan(0)
    }
  })
})
