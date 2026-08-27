import { describe, expect, it, vi } from 'vitest'

import { extractAgentNavigations, isNavigationPathAllowed } from './agentNavigation'

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
