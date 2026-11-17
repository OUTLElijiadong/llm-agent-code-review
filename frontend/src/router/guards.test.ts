import { createMemoryHistory, createRouter, type RouteLocationNormalized, type Router } from 'vue-router'
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
const workspace = vi.hoisted(() => ({
  activeSpace: 'personal',
  activeEnterpriseId: null as number | null,
  syncIdentity: vi.fn(),
  activatePersonal: vi.fn(() => { workspace.activeSpace = 'personal'; workspace.activeEnterpriseId = null }),
  activateEnterprise: vi.fn((id?: number | null) => {
    workspace.activeSpace = 'enterprise'
    if (id !== undefined) workspace.activeEnterpriseId = id
  }),
  refreshEnterpriseMembership: vi.fn(async (): Promise<boolean | null> => false),
}))

vi.mock('@/stores/user', () => ({
  useUserStore: () => auth.user,
}))
vi.mock('@/stores/workspace', () => ({ useWorkspaceStore: () => workspace }))

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
    push: vi.fn(),
    replace: vi.fn(),
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
beforeEach(() => {
  workspace.activeSpace = 'personal'
  workspace.activeEnterpriseId = null
  workspace.syncIdentity.mockReset()
  workspace.activatePersonal.mockClear()
  workspace.activateEnterprise.mockClear()
  workspace.refreshEnterpriseMembership.mockReset().mockResolvedValue(false)
})

describe('router guards', () => {
  it('allows public pages and redirects an authenticated login visit to role home', async () => {
    /** 验证公开页放行与已登录用户访问登录页的重定向。 */
    const { before } = installHarness()
    expect(await before(route('/about', { public: true }), route('/'))).toBe(true)

    auth.user.token = 'token'
    auth.user.profile = { id: 1, role: 'admin' }
    expect(await before(route('/login', { public: true }), route('/'))).toEqual({
      path: '/admin/operations?section=overview',
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

  it('enforces legacy roles while allowing admins to operate authenticated user pages', async () => {
    /** 管理小菱需要跨管理端与用户端工作，管理员不应被二次重定向打断。 */
    const { before } = installHarness()
    auth.user.token = 'token'
    auth.user.profile = { id: 1, role: 'admin' }
    auth.user.isAdmin.mockReturnValue(true)

    expect(await before(route('/admin/users', { role: 'reviewer' }), route('/'))).toBe(true)
    expect(await before(route('/projects'), route('/'))).toBe(true)
    expect(await before(route('/sandboxes'), route('/'))).toBe(true)
    expect(await before(route('/agent-studio'), route('/'))).toBe(true)
    expect(await before(route('/projects/1'), route('/'))).toBe(true)
    expect(await before(route('/reviews/2'), route('/'))).toBe(true)
    expect(await before(route('/code/3/file/4'), route('/'))).toBe(true)
    expect(await before(route('/reports/5'), route('/'))).toBe(true)

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

  it('rejects ordinary users from reviewer-only Agent Studio even if a stale permission remains', async () => {
    const { before } = installHarness()
    auth.user.token = 'token'
    auth.user.profile = { id: 8, role: 'user' }
    auth.user.hasPermission.mockReturnValue(true)

    expect(
      await before(
        route('/agent-studio', { roles: ['reviewer', 'admin'], permissions: ['agent_asset:create'] }),
        route('/'),
      ),
    ).toEqual({ path: '/403' })
  })

  it('lets RBAC admins bypass roles and permissions', async () => {
    /** 验证 isAdmin 对新 RBAC 元数据检查的绕过。 */
    const { before } = installHarness()
    auth.user.token = 'token'
    auth.user.profile = { id: 1, role: 'reviewer' }
    auth.user.isAdmin.mockReturnValue(true)

    expect(
      await before(
        route('/admin/agents', { roles: ['missing'], permissions: ['missing:permission'] }),
        route('/'),
      ),
    ).toBe(true)
    expect(auth.user.hasRole).not.toHaveBeenCalled()
    expect(auth.user.hasPermission).not.toHaveBeenCalled()
  })

  it('企业上下文拦截个人路由，显式进入企业路由会设置企业模式', async () => {
    const { before } = installHarness()
    auth.user.token = 'token'
    auth.user.profile = { id: 7, role: 'user' }
    workspace.activeSpace = 'enterprise'

    expect(await before(route('/forum', { workspace: 'personal' }), route('/enterprise')))
      .toEqual({ path: '/enterprise', replace: true })
    expect(await before(route('/enterprise', { workspace: 'enterprise' }), route('/dashboard'))).toBe(true)
    expect(workspace.activateEnterprise).toHaveBeenCalledOnce()
  })

  it('企业成员即使切回个人空间也不能进入公开论坛', async () => {
    const { before } = installHarness()
    auth.user.token = 'token'
    auth.user.profile = { id: 8, role: 'user' }
    workspace.activeSpace = 'personal'
    workspace.refreshEnterpriseMembership.mockResolvedValue(true)

    expect(await before(route('/forum', { workspace: 'personal', personalAccountOnly: true }), route('/dashboard')))
      .toEqual({ path: '/enterprise', replace: true })
    expect(workspace.refreshEnterpriseMembership).toHaveBeenCalledExactlyOnceWith(true)
  })

  it('无法确认账号类型时阻止打开公开论坛', async () => {
    const { before } = installHarness()
    auth.user.token = 'token'
    auth.user.profile = { id: 9, role: 'user' }
    workspace.refreshEnterpriseMembership.mockResolvedValue(null)

    expect(await before(route('/forum', { workspace: 'personal', personalAccountOnly: true }), route('/dashboard')))
      .toEqual({ path: '/enterprise', replace: true })
  })

  it('较旧的论坛资格请求返回时不得重定向较新的导航', async () => {
    const { before } = installHarness()
    auth.user.token = 'token'
    auth.user.profile = { id: 9, role: 'user' }
    let finishOld!: (value: boolean | null) => void
    workspace.refreshEnterpriseMembership
      .mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve }))
      .mockResolvedValueOnce(false)
    const oldNavigation = before(route('/forum', { personalAccountOnly: true }), route('/dashboard'))
    const newNavigation = before(route('/forum/7', { personalAccountOnly: true }), route('/dashboard'))
    expect(await newNavigation).toBe(true)
    finishOld(null)
    expect(await oldNavigation).toBe(false)
  })

  it('用户重复选择当前页面时，等待中的旧论坛导航不得覆盖该意图', async () => {
    auth.user.token = 'token'
    auth.user.profile = { id: 9, role: 'user' }
    let finishOld!: (value: boolean | null) => void
    workspace.refreshEnterpriseMembership.mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve }))
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/dashboard', component: {} },
        { path: '/forum', component: {}, meta: { personalAccountOnly: true } },
        { path: '/enterprise', component: {} },
      ],
    })
    setupGuards(router)
    await router.push('/dashboard')
    const oldNavigation = router.push('/forum')
    await vi.waitFor(() => expect(workspace.refreshEnterpriseMembership).toHaveBeenCalledOnce())
    await router.push('/dashboard')
    finishOld(true)
    await oldNavigation
    expect(router.currentRoute.value.path).toBe('/dashboard')
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
