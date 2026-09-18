import { describe, expect, it } from 'vitest'

import router from './index'

describe('Agent Studio route permissions', () => {
  it('Agent 工坊仅审查者及以上可进入(049 迁移同步收回普通用户权限)', () => {
    const route = router.getRoutes().find((item) => item.name === 'AgentStudio')

    expect(route).toBeDefined()
    expect(route?.meta.roles).not.toContain('user')
    expect(route?.meta.roles).toContain('reviewer')
    expect(route?.meta.permissions).toContain('agent_asset:create')
  })
})

describe('authenticated route visibility permissions', () => {
  const expected: Record<string, string> = {
    ProjectList: 'project:view',
    ProjectDetail: 'project:view',
    CodeHub: 'file:view',
    CodeFileList: 'file:view',
    ReviewTaskList: 'review:view',
    ReviewTaskDetail: 'review:view',
    ReviewStart: 'review:start',
    IssueHub: 'issue:view',
    RuleConfigLegacy: 'rule:view',
    ReportList: 'report:view',
    ReportDetail: 'report:view',
    AgentCenter: 'agent:view',
    SecurityCenter: 'security:view',
  }

  it('declares the read permission that controls every permission-backed navigation entry', () => {
    const routes = router.getRoutes()
    for (const [name, permission] of Object.entries(expected)) {
      const route = routes.find((item) => item.name === name)
      expect(route, name).toBeDefined()
      expect(route?.meta.permissions, name).toContain(permission)
    }
  })

  it('旧审查规则路由兼容跳转到统一页面的规则标签', () => {
    const route = router.getRoutes().find((item) => item.name === 'RuleConfigLegacy')
    expect(route?.redirect).toBeTypeOf('function')
    const redirect = (route?.redirect as (to: { query: Record<string, string> }) => unknown)({ query: { q: 'sql' } })
    expect(redirect).toEqual({ path: '/security', query: { q: 'sql', tab: 'rules' } })
  })

  it('旧管理员报告模板路由跳转到唯一正式页面', () => {
    const legacy = router.getRoutes().find((item) => item.name === 'AdminReportTemplateLegacy')
    const canonical = router.getRoutes().find((item) => item.name === 'ReportTemplateManage')
    expect(legacy?.redirect).toBe('/report/templates')
    expect(canonical?.path).toBe('/report/templates')
  })
})
