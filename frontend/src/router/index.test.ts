import { describe, expect, it } from 'vitest'

import router from './index'

describe('Agent Studio route permissions', () => {
  it('limits Agent Studio to reviewers and admins', () => {
    const route = router.getRoutes().find((item) => item.name === 'AgentStudio')

    expect(route).toBeDefined()
    expect(route?.meta.roles).toEqual(['reviewer', 'admin'])
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
})
