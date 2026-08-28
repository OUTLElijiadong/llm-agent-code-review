import { describe, expect, it, vi } from 'vitest'

import {
  extractAgentNavigations,
  isNavigationPathAllowed,
  resolveLocalNavigationRequest,
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
      {
        path: '/admin',
        children: [
          { path: 'audit', meta: { title: '系统操作审计', role: 'admin' } },
          { path: 'users', meta: { title: '用户管理', role: 'admin', permissions: ['user:view'] } },
        ],
      },
      { path: '/projects', meta: { title: '项目管理', permissions: ['project:view'] } },
      { path: '/reviews', meta: { title: '审查记录', permissions: ['review:view'] } },
    ],
  }

  function routerFor(path: string) {
    return {
      options: routeOptions,
      resolve: () => ({
        matched: [{ path }],
        meta: path === '/admin/audit'
          ? { title: '系统操作审计', role: 'admin' }
          : path === '/admin/users'
            ? { title: '用户管理', role: 'admin', permissions: ['user:view'] }
            : path === '/reviews'
              ? { title: '审查记录', permissions: ['review:view'] }
            : { title: '项目管理', permissions: ['project:view'] },
      }),
    }
  }

  it('resolves an explicit navigation request without requiring a model round', () => {
    const result = resolveLocalNavigationRequest(
      '请只执行页面导航：打开系统操作审计页面。不要查询、修改、删除或执行任何运维操作。',
      routerFor('/admin/audit') as never,
      { ...guard(true), isAdmin: () => true },
    )

    expect(result).toEqual({
      kind: 'navigate',
      directive: {
        action: 'navigate',
        route: '/admin/audit',
        label: '系统操作审计',
      },
    })
  })

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
