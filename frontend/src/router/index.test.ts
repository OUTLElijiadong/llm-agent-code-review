import { describe, expect, it } from 'vitest'

import router from './index'

describe('Agent Studio route permissions', () => {
  it('allows ordinary members with the private draft permission to enter Agent Studio', () => {
    const route = router.getRoutes().find((item) => item.name === 'AgentStudio')

    expect(route).toBeDefined()
    expect(route?.meta.roles).toContain('user')
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
