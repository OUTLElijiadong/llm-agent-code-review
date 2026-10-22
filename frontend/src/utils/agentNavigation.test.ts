import { describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import {
  extractAgentNavigations,
  isNavigationPathAllowed,
  resolveLocalNavigationRequest,
  renderAuthorizedAgentMarkdown,
} from './agentNavigation'

function guard(permission: boolean) {
  return {
    token: 'token',
    isAdmin: () => false,
    isSuperAdmin: () => false,
    hasRole: (role: string): boolean => role === 'user',
    hasPermission: () => permission,
  }
}

describe('shared navigation visibility', () => {
  it('fails closed for unknown, external and unauthorized routes', () => {
    const resolve = vi.fn(({ path }: { path: string }) => ({
      matched: path === '/known' ? [{}] : [],
      meta: { permissions: ['known:view'] },
    }))
    const router = { resolve }

    expect(isNavigationPathAllowed(router as never, '/known', guard(false))).toBe(false)
    expect(isNavigationPathAllowed(router as never, '/missing', guard(true))).toBe(false)
    expect(isNavigationPathAllowed(router as never, '//evil.example', guard(true))).toBe(false)
  })

  it('shows a registered route after its guard requirements pass', () => {
    const router = {
      resolve: () => ({ matched: [{}], meta: { permissions: ['known:view'] } }),
    }

    expect(isNavigationPathAllowed(router as never, '/known', guard(true))).toBe(true)
  })

  it('rejects a model-invented path resolved by the catch-all route', () => {
    const router = {
      resolve: () => ({
        matched: [{ path: '/:pathMatch(.*)*' }],
        meta: { public: true },
      }),
    }

    expect(isNavigationPathAllowed(router as never, '/model-invented-page', guard(true))).toBe(false)
  })

  it('matches legacy single-role routes with the router guard semantics', () => {
    const router = {
      resolve: () => ({ matched: [{ path: '/reviewer-only' }], meta: { role: 'reviewer' } }),
    }
    const reviewerGuard = guard(true)
    reviewerGuard.hasRole = (role: string): boolean => role === 'reviewer'

    expect(isNavigationPathAllowed(router as never, '/reviewer-only', reviewerGuard)).toBe(true)
  })

  it('历史 AI 导航文本不把危险协议或 SVG 变成可点击入口', () => {
    const router = {
      resolve: ({ path }: { path: string }) => ({
        matched: path === '/known' ? [{ path }] : [],
        meta: { permissions: ['known:view'] },
      }),
    }
    const html = renderAuthorizedAgentMarkdown(
      '[安全](/known) [无权](/admin) [脚本](javascript:alert%281%29) '
      + '[数据](data:text/html;base64,PHN2Zz4=) <svg onload="alert(1)"></svg>',
      router as never,
      guard(true),
    )
    const root = document.createElement('div')
    root.innerHTML = html
    expect([...root.querySelectorAll('a')].map((link) => link.getAttribute('href'))).toEqual(['/known'])
    expect(root.querySelector('svg, script, [onload]')).toBeNull()
  })

  it('只把规范论坛详情路径渲染为链接，不自动链接裸路径或未知动态路径', () => {
    const router = {
      resolve: ({ path }: { path: string }) => {
        if (/^\/forum\/[1-9]\d*$/.test(path)) {
          return { matched: [{ path: '/forum/:id' }], meta: {} }
        }
        if (/^\/forum\/[1-9]\d*\/edit$/.test(path)) {
          return { matched: [{ path: '/forum/:id/edit' }], meta: {} }
        }
        if (path === '/forum/new') return { matched: [{ path: '/forum/new' }], meta: {} }
        return { matched: [], meta: {} }
      },
    }
    const html = renderAuthorizedAgentMarkdown(
      '[第一篇](/forum/44)\n[第二篇](/forum/43)\n[第三篇](/forum/42)\n'
      + '裸路径 /forum/44；[非数字 ID](/forum/not-a-post) [零 ID](/forum/0) '
      + '[伪造页面](/forum/admin) [未登记路径](/forum/999/edit/extra) '
      + '[添加参数](/forum/44?next=/admin)',
      router as never,
      guard(true),
    )
    const root = document.createElement('div')
    root.innerHTML = html

    expect([...root.querySelectorAll('a')].map((link) => [link.textContent, link.getAttribute('href')])).toEqual([
      ['第一篇', '/forum/44'],
      ['第二篇', '/forum/43'],
      ['第三篇', '/forum/42'],
    ])
    expect(root.textContent).toContain('裸路径 /forum/44')
    expect(root.textContent).not.toContain('非数字 ID')
    expect(root.textContent).not.toContain('零 ID')
    expect(root.textContent).not.toContain('伪造页面')
    expect(root.textContent).not.toContain('未登记路径')
    expect(root.textContent).not.toContain('添加参数')
  })
})

describe('navigation fallback extraction', () => {
  const router = {
    options: {
      routes: [
        {
          path: '/admin',
          children: [
            { path: 'audit', meta: { title: '系统操作审计' } },
          ],
        },
      ],
    },
  }

  it('turns a known bare route in navigation context into a button directive', () => {
    const result = extractAgentNavigations(
      '页面导航\n页面名称：系统操作审计\n路由：/admin/audit\n你可以直接进入该页面。',
      router as never,
    )

    expect(result.directives).toEqual([
      { action: 'navigate', route: '/admin/audit', label: '系统操作审计' },
    ])
  })

  it('ignores unknown routes and routes shown only inside code fences', () => {
    const result = extractAgentNavigations(
      '页面路由：/model-invented\n```text\n页面路由：/admin/audit\n```',
      router as never,
    )

    expect(result.directives).toEqual([])
  })
})

describe('local navigation cost guard', () => {
  const routeOptions = {
    routes: [
      { path: '/audit', meta: { title: '操作审计', roles: ['reviewer'], permissions: ['audit:view'] } },
      {
        path: '/admin',
        children: [
          { path: 'audit', meta: { title: '系统操作审计', role: 'admin' } },
          { path: 'platform', meta: { title: '平台配置中心', role: 'admin' } },
          { path: 'operations', meta: { title: '运行监控', role: 'admin' } },
          { path: 'users', meta: { title: '用户管理', role: 'admin', permissions: ['user:view'] } },
          { path: 'embedding', meta: { title: 'RAG 嵌入配置', role: 'admin', superAdmin: true } },
          { path: 'mcp-workers', meta: { title: 'MCP 与沙箱节点', role: 'admin', superAdmin: true } },
          { path: 'llm', meta: { title: '大模型配置', role: 'admin', superAdmin: true } },
        ],
      },
      { path: '/projects', meta: { title: '项目管理', permissions: ['project:view'] } },
      { path: '/reviews', meta: { title: '审查记录', permissions: ['review:view'] } },
    ],
  }

  function routerFor(_path: string) {
    return createRouter({ history: createMemoryHistory(), routes: routeOptions.routes.map(route => ({
      component: {}, ...route, children: route.children?.map(child => ({ component: {}, ...child })),
    })) })
  }

  it('resolves an explicit navigation request without requiring a model round', () => {
    const result = resolveLocalNavigationRequest(
      '请只执行页面导航：打开系统操作审计页面。不要查询、修改、删除或执行任何运维操作。',
      routerFor('/audit') as never,
      { ...guard(true), isAdmin: () => true },
    )

    expect(result).toEqual({
      kind: 'navigate',
      directive: {
        action: 'navigate',
        route: '/audit',
        label: '操作审计',
      },
    })
  })

  it('审查员打开系统操作审计时导航到 audit:view 页面', () => {
    const result = resolveLocalNavigationRequest(
      '打开系统操作审计页面',
      routerFor('/audit') as never,
      { ...guard(true), hasRole: (role: string) => role === 'reviewer' },
    )

    expect(result).toEqual({
      kind: 'navigate',
      directive: { action: 'navigate', route: '/audit', label: '操作审计' },
    })
  })

  it.each(['打开审计', '打开审计页面'])('审查员输入“%s”选择可访问的审计页，不误派管理端', content => {
    const result = resolveLocalNavigationRequest(
      content,
      routerFor('/audit') as never,
      {
        ...guard(true),
        hasRole: (role: string) => role === 'reviewer',
      },
    )

    expect(result).toEqual({
      kind: 'navigate',
      directive: { action: 'navigate', route: '/audit', label: '操作审计' },
    })
  })

  it('明确要求管理后台审计时仍选择管理路由并拒绝普通用户', () => {
    const result = resolveLocalNavigationRequest(
      '打开管理后台审计页面',
      routerFor('/admin/operations') as never,
      { ...guard(true), hasRole: (role: string) => role === 'reviewer' },
    )

    expect(result).toEqual({ kind: 'forbidden' })
  })

  it('管理员明确要求管理后台审计时进入审计分区，不能误开运行总览', () => {
    expect(resolveLocalNavigationRequest('打开管理后台审计页面', routerFor('/admin/audit'),
      { ...guard(true), isAdmin: () => true })).toEqual({
      kind: 'navigate', directive: { action: 'navigate', route: '/admin/audit', label: '系统操作审计' },
    })
  })

  describe.each(['管理后台的', '管理端的', '管理员的'])('中文管理限定词 %s', qualifier => {
    it.each([
      ['平台配置', '/admin/platform', '平台配置中心'],
      ['审计', '/admin/audit', '系统操作审计'],
    ])('无权账号打开%s直接禁止，不降级同名用户入口', (target, _route, _label) => {
      const actor = { ...guard(true), hasRole: (role: string) => role === 'reviewer' }
      expect(resolveLocalNavigationRequest(`打开${qualifier}${target}`, routerFor('/audit'), actor))
        .toEqual({ kind: 'forbidden' })
    })

    it.each([
      ['平台配置', '/admin/platform', '平台配置中心'],
      ['审计', '/admin/audit', '系统操作审计'],
    ])('具备管理权限打开%s准确选择管理路径', (target, route, label) => {
      expect(resolveLocalNavigationRequest(`打开${qualifier}${target}`, routerFor(route),
        { ...guard(true), isAdmin: () => true })).toEqual({
        kind: 'navigate', directive: { action: 'navigate', route, label },
      })
    })

    it.each([
      ['平台配置', '修改默认模型'],
      ['审计', '删除审计日志'],
    ])('打开%s同时要求%s时不得当作纯导航吞掉写入命令', (target, operation) => {
      expect(resolveLocalNavigationRequest(`打开${qualifier}${target}并${operation}`, routerFor('/admin/platform'),
        { ...guard(true), isAdmin: () => true })).toBeNull()
    })
  })

  it.each([false, true])('没有管理限定词的同名审计入口保持用户路径，管理员=%s', isAdmin => {
    expect(resolveLocalNavigationRequest('打开审计', routerFor('/audit'), {
      ...guard(true), isAdmin: () => isAdmin, hasRole: (role: string) => role === 'reviewer',
    })).toEqual({
      kind: 'navigate', directive: { action: 'navigate', route: '/audit', label: '操作审计' },
    })
  })

  it('未加管理限定词的平台配置仍按管理入口权限拒绝普通用户', () => {
    expect(resolveLocalNavigationRequest('打开平台配置', routerFor('/admin/platform'), guard(true)))
      .toEqual({ kind: 'forbidden' })
  })

  it.each(['/admin/embedding', '/admin/mcp-workers', '/admin/llm'])(
    '普通管理员不能本地导航到最高管理员页面 %s',
    (route) => {
      const result = resolveLocalNavigationRequest(
        `打开${route === '/admin/embedding' ? 'RAG 嵌入配置' : route === '/admin/mcp-workers' ? 'MCP 与沙箱节点' : '大模型配置'}页面`,
        routerFor(route) as never,
        { ...guard(true), isAdmin: () => true, isSuperAdmin: () => false },
      )

      expect(result).toEqual({ kind: 'forbidden' })
    },
  )

  it('does not intercept a request that combines navigation with an operation', () => {
    const result = resolveLocalNavigationRequest(
      '打开系统操作审计页面并检查最近的高风险操作',
      routerFor('/admin/audit') as never,
      { ...guard(true), isAdmin: () => true },
    )

    expect(result).toBeNull()
  })

  it('returns a non-action result for an unauthorized target so no hidden link is rendered', () => {
    const result = resolveLocalNavigationRequest(
      '打开用户管理页面',
      routerFor('/admin/users') as never,
      guard(false),
    )

    expect(result).toEqual({ kind: 'forbidden' })
  })

  it('把侧边栏显示名“审查任务”解析为已有审查记录路由', () => {
    const result = resolveLocalNavigationRequest(
      '打开审查任务页面',
      routerFor('/reviews') as never,
      guard(true),
    )

    expect(result).toEqual({
      kind: 'navigate',
      directive: {
        action: 'navigate',
        route: '/reviews',
        label: '审查记录',
      },
    })
  })
})


describe('真实管理入口重定向权限', () => {
  const routes = [{ path: '/admin', meta: { role: 'admin' }, children: [
    { path: 'platform', component: {}, meta: { title: '平台配置中心' } },
    ...['embedding', 'mcp-workers', 'llm'].map(section => ({
      path: section, redirect: { path: '/admin/platform', query: { section } },
      meta: { superAdmin: true },
    })),
  ] }]
  const router = createRouter({ history: createMemoryHistory(), routes })
  it.each(['embedding', 'mcp-workers', 'llm'])('普通管理员不能通过旧入口或 section 查询进入 %s', section => {
    const actor = { ...guard(true), isAdmin: () => true }
    expect(isNavigationPathAllowed(router, `/admin/${section}`, actor)).toBe(false)
    expect(isNavigationPathAllowed(router, `/admin/platform?section=${section}`, actor)).toBe(false)
    expect(isNavigationPathAllowed(router, '/admin/platform?section=report-templates', actor)).toBe(true)
    const superAdmin = { ...actor, isSuperAdmin: () => true }
    expect(isNavigationPathAllowed(router, `/admin/${section}`, superAdmin)).toBe(true)
    expect(isNavigationPathAllowed(router, `/admin/platform?section=${section}`, superAdmin)).toBe(true)
  })
})
