import type { RouteLocationNormalized, Router } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const auth = vi.hoisted(() => ({
  user: {
    token: '',
    profile: null as null | { id: number; role: string },
    fetchProfile: vi.fn(),
    logout: vi.fn(),
    clearSession: vi.fn(),
    isAdmin: vi.fn(() => false),
    hasRole: vi.fn<(role: string) => boolean>(() => false),
    hasPermission: vi.fn<(permission: string) => boolean>(() => false),
  },
}))

vi.mock('@/stores/user', () => ({
  useUserStore: () => auth.user,
}))

import { setupGuards } from './guards'

type BeforeGuard = (
  to: RouteLocationNormalized,
  from: RouteLocationNormalized,
) => Promise<any> | any

type AfterGuard = (to: RouteLocationNormalized) => void

/** 构造可捕获 beforeEach/afterEach 回调的 Router 测试桩。 */
function installHarness(): { before: BeforeGuard; after: AfterGuard } {
  let before: BeforeGuard | undefined
  let after: AfterGuard | undefined
  const router = {
    beforeEach: vi.fn((guard: BeforeGuard) => {
      before = guard
    }),
    afterEach: vi.fn((guard: AfterGuard) => {
      after = guard
    }),
  }
  setupGuards(router as unknown as Router)
  return { before: before!, after: after! }
}

/** 构造守卫所需的最小 RouteLocationNormalized。 */
function route(path: string, meta: Record<string, any> = {}, fullPath = path): RouteLocationNormalized {
  return { path, fullPath, meta } as unknown as RouteLocationNormalized
}

/** 重置用户守卫测试桩。 */
function resetAuthHarness(): void {
  auth.user.token = ''
  auth.user.profile = null
  auth.user.fetchProfile.mockReset()
  auth.user.logout.mockReset()
  auth.user.isAdmin.mockReset().mockReturnValue(false)
  auth.user.hasRole.mockReset().mockReturnValue(false)
  auth.user.hasPermission.mockReset().mockReturnValue(false)
}

beforeEach(resetAuthHarness)

describe('router guards', () => {
  it('allows public pages and redirects an authenticated login visit to role home', async () => {
    /** 验证公开页放行与已登录用户访问登录页的重定向。 */
    const { before } = installHarness()
    expect(await before(route('/about', { public: true }), route('/'))).toBe(true)

    auth.user.token = 'token'
    auth.user.profile = { id: 1, role: 'admin' }
    expect(await before(route('/login', { public: true }), route('/'))).toEqual({
      path: '/admin/overview',
      replace: true,
    })
  })

  it('recovers a public login profile or logs out when recovery fails', async () => {
    /** 验证公开登录页的 token 恢复成功和失败分支。 */
    const { before } = installHarness()
    auth.user.token = 'token'
    auth.user.fetchProfile.mockImplementationOnce(async () => {
      auth.user.profile = { id: 7, role: 'reviewer' }
    })

    expect(await before(route('/register', { public: true }), route('/'))).toEqual({
      path: '/dashboard',
      replace: true,
    })

    auth.user.profile = null
    auth.user.fetchProfile.mockRejectedValueOnce(new Error('expired'))
    expect(await before(route('/login', { public: true }), route('/'))).toBe(true)
    expect(auth.user.clearSession).toHaveBeenCalledOnce()
  })

  it('redirects unauthenticated users with the original full path', async () => {
    /** 验证私有页未登录重定向保留 query。 */
    const { before } = installHarness()

    expect(await before(route('/projects', {}, '/projects?page=2'), route('/'))).toEqual({
      path: '/login',
      query: { redirect: '/projects?page=2' },
    })
  })

  it('restores a private profile, redirects root and rejects an expired token', async () => {
    /** 验证私有页 profile 恢复、根路径分流与恢复失败。 */
    const { before } = installHarness()
    auth.user.token = 'token'
    auth.user.fetchProfile.mockImplementationOnce(async () => {
      auth.user.profile = { id: 7, role: 'user' }
    })

    expect(await before(route('/'), route('/login'))).toEqual({ path: '/dashboard', replace: true })

    auth.user.profile = null
    auth.user.fetchProfile.mockRejectedValueOnce(new Error('expired'))
    expect(await before(route('/projects', {}, '/projects'), route('/'))).toEqual({
      path: '/login',
      query: { redirect: '/projects' },
    })
    expect(auth.user.clearSession).toHaveBeenCalledOnce()
  })

  it('enforces legacy role metadata for users while allowing admins to bypass it', async () => {
    /** 验证普通用户受历史 role 限制，RBAC 管理员保持超级用户语义。 */
    const { before } = installHarness()
    auth.user.token = 'token'
    auth.user.profile = { id: 1, role: 'admin' }
    auth.user.isAdmin.mockReturnValue(true)

    expect(await before(route('/admin/users', { role: 'reviewer' }), route('/'))).toBe(true)
    expect(await before(route('/projects'), route('/'))).toBe(true)
    expect(await before(route('/code'), route('/'))).toBe(true)
    expect(await before(route('/rules'), route('/'))).toBe(true)
    expect(await before(route('/knowledge'), route('/'))).toBe(true)
    expect(await before(route('/profile/personalization'), route('/'))).toBe(true)
    expect(await before(route('/security'), route('/'))).toBe(true)

    auth.user.profile = { id: 2, role: 'user' }
    auth.user.isAdmin.mockReturnValue(false)
    auth.user.hasRole.mockReturnValue(false)
    expect(await before(route('/admin/users', { role: 'reviewer' }), route('/'))).toEqual({ path: '/403' })
  })

  it('requires any configured role and permission while allowing valid access', async () => {
    /** 验证 roles/permissions 任一匹配语义与拒绝路径。 */
    const { before } = installHarness()
    auth.user.token = 'token'
    auth.user.profile = { id: 7, role: 'user' }

    expect(await before(route('/review', { roles: ['reviewer'] }), route('/'))).toEqual({ path: '/403' })
    auth.user.hasRole.mockImplementation((role: string) => role === 'user')
    expect(await before(route('/review', { roles: ['reviewer', 'user'] }), route('/'))).toBe(true)

    expect(await before(route('/reports', { permissions: ['report:view'] }), route('/'))).toEqual({ path: '/403' })
    auth.user.hasPermission.mockImplementation((permission: string) => permission === 'report:view')
    expect(await before(route('/reports', { permissions: ['report:view'] }), route('/'))).toBe(true)
  })

  it('lets RBAC admins bypass roles and permissions', async () => {
    /** 验证 isAdmin 对新 RBAC 元数据检查的绕过。 */
    const { before } = installHarness()
    auth.user.token = 'token'
    auth.user.profile = { id: 1, role: 'reviewer' }
    auth.user.isAdmin.mockReturnValue(true)

    expect(
      await before(
        route('/agents', { roles: ['missing'], permissions: ['missing:permission'] }),
        route('/'),
      ),
    ).toBe(true)
    expect(auth.user.hasRole).not.toHaveBeenCalled()
    expect(auth.user.hasPermission).not.toHaveBeenCalled()
  })

  it('updates the document title after navigation', () => {
    /** 验证带标题和无标题路由的浏览器标题。 */
    const { after } = installHarness()

    after(route('/projects', { title: '项目管理' }))
    expect(document.title).toBe('项目管理 - 棱镜 Prism')

    after(route('/unknown'))
    expect(document.title).toBe('棱镜 Prism')
  })
})
