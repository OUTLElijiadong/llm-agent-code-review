import { describe, expect, it } from 'vitest'

import router from './index'

describe('Agent Studio route permissions', () => {
  it('个人 Agent 页面对登录用户开放并限定个人空间', () => {
    const route = router.getRoutes().find(item => item.name === 'PersonalAgents')
    expect(route?.path).toBe('/my-agents')
    expect(route?.meta.workspace).toBe('personal')
    expect(route?.meta.roles).toBeUndefined()
    expect(route?.meta.permissions).toBeUndefined()
  })
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

  it('服务器安全中心是仅超级管理员可进入的独立页面', () => {
    const route = router.getRoutes().find((item) => item.name === 'AdminSecurityCenter')
    expect(route?.path).toBe('/admin/security-center')
    expect(route?.meta.superAdmin).toBe(true)
  })

  it('渠道账单导入和人工审阅只允许平台最高管理员进入', () => {
    const route = router.getRoutes().find((item) => item.name === 'AdminBillingStatements')
    expect(route?.path).toBe('/admin/billing-statements')
    expect(route?.meta.superAdmin).toBe(true)
    expect(route?.meta.roles).toEqual(['admin'])
  })

  it('模型账本结算与未发送释放工作台只允许平台最高管理员进入', () => {
    const route = router.getRoutes().find((item) => item.name === 'AdminBillingReservations')
    expect(route?.path).toBe('/admin/billing-reservations')
    expect(route?.meta.superAdmin).toBe(true)
    expect(route?.meta.roles).toEqual(['admin'])
  })

  it('账本待复核页允许平台管理员进入，接口进一步校验实际委任', () => {
    const route = router.getRoutes().find((item) => item.name === 'AdminBillingReviews')
    expect(route?.path).toBe('/admin/billing-reviews')
    expect(route?.meta.roles).toEqual(['admin'])
    expect(route?.meta.superAdmin).not.toBe(true)
  })

  it('费率待复核页允许平台管理员进入，由接口检查独立授权', () => {
    const route = router.getRoutes().find((item) => item.name === 'AdminBillingRateReviews')
    expect(route?.path).toBe('/admin/billing-rate-reviews')
    expect(route?.meta.roles).toEqual(['admin'])
    expect(route?.meta.superAdmin).not.toBe(true)
  })

  it('退款待审核页允许平台管理员进入，由接口检查独立授权', () => {
    const route = router.getRoutes().find((item) => item.name === 'AdminBillingRefundReviews')
    expect(route?.path).toBe('/admin/billing-refund-reviews')
    expect(route?.meta.roles).toEqual(['admin'])
    expect(route?.meta.superAdmin).not.toBe(true)
  })

  it('模型费率版本与双人审批工作台只允许平台最高管理员进入', () => {
    const route = router.getRoutes().find((item) => item.name === 'AdminBillingPricing')
    expect(route?.path).toBe('/admin/billing-pricing')
    expect(route?.meta.superAdmin).toBe(true)
    expect(route?.meta.roles).toEqual(['admin'])
  })

  it('全局共享目录对登录用户开放，发布审批只允许平台最高管理员进入', () => {
    const routes = router.getRoutes()
    const catalog = routes.find((item) => item.name === 'GlobalEnterpriseAssetCatalog')
    const review = routes.find((item) => item.name === 'EnterpriseAssetPublicationReview')
    expect(catalog?.path).toBe('/agent-assets')
    expect(catalog?.meta.permissions).toBeUndefined()
    expect(catalog?.meta.workspace).toBeUndefined()
    expect(catalog?.meta.roles).toBeUndefined()
    expect(review?.path).toBe('/admin/enterprise-asset-publications')
    expect(review?.meta.superAdmin).toBe(true)
  })

  it('个人论坛、个人知识和个人用量声明个人空间，企业沟通入口声明企业空间', () => {
    const routes = router.getRoutes()
    for (const name of ['ForumList', 'ForumPostCreate', 'ForumPostDetail', 'ForumPostEdit', 'KnowledgeBase', 'PersonalTokenUsage']) {
      expect(routes.find((item) => item.name === name)?.meta.workspace, name).toBe('personal')
    }
    expect(routes.find((item) => item.name === 'EnterpriseCommunication')?.meta.workspace).toBe('enterprise')
  })
})
