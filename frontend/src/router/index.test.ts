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
    RuleConfig: 'rule:view',
    ReportList: 'report:view',
    ReportDetail: 'report:view',
    AuditLogs: 'audit:view',
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

  it('旧审查规则入口兼容跳转到安全与审查规则中心', () => {
    const route = router.getRoutes().find((item) => item.name === 'RuleConfig')
    expect(route?.path).toBe('/rules')
    expect(route?.redirect).toEqual({ path: '/security', query: { section: 'rules' } })
  })

  it('后台报告模板入口兼容跳转到平台配置中心的报告模板标签', () => {
    const legacy = router.getRoutes().find((item) => item.name === 'AdminReportTemplateManage')
    const canonical = router.getRoutes().find((item) => item.name === 'ReportTemplateManage')
    expect(legacy?.redirect).toEqual({ path: '/admin/platform', query: { section: 'report-templates' } })
    expect(canonical?.path).toBe('/report/templates')
  })

  it('操作审计作为审查员权限页面独立于管理员路由开放', () => {
    const route = router.getRoutes().find((item) => item.name === 'AuditLogs')

    expect(route?.path).toBe('/audit')
    expect(route?.meta.roles).toContain('reviewer')
    expect(route?.meta.permissions).toContain('audit:view')
  })
})
