import { createPinia, setActivePinia } from 'pinia'
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  authLogin: vi.fn(),
  authRegister: vi.fn(),
  authMe: vi.fn(),
  fetchRoles: vi.fn(),
  fetchPermissions: vi.fn(),
  fetchMenus: vi.fn(),
  fetchDataScope: vi.fn(),
  getToken: vi.fn<() => string | null>(() => null),
  setToken: vi.fn(),
  clearToken: vi.fn(),
}))

vi.mock('@/api/auth', () => ({
  login: api.authLogin,
  register: api.authRegister,
  me: api.authMe,
}))

vi.mock('@/api/rbac', () => ({
  fetchUserRoles: api.fetchRoles,
  fetchUserPermissions: api.fetchPermissions,
  fetchUserMenus: api.fetchMenus,
  fetchUserDataScope: api.fetchDataScope,
}))

vi.mock('@/utils/token', () => ({
  getToken: api.getToken,
  setToken: api.setToken,
  clearToken: api.clearToken,
}))

import { useUserStore } from './user'

let store: ReturnType<typeof useUserStore>

const member = {
  id: 7,
  username: 'alice',
  nickname: 'Alice',
  role: 'user',
  status: 1,
}

/** 重置单例 Store 状态并提供默认成功的 RBAC API。 */
function resetUserStore(): void {
  store.$patch({
    token: '',
    profile: null,
    permissions: new Set<string>(),
    roles: [],
    menus: [],
    dataScope: null,
  })
  api.authLogin.mockReset()
  api.authRegister.mockReset()
  api.authMe.mockReset()
  api.fetchRoles.mockReset().mockResolvedValue([])
  api.fetchPermissions.mockReset().mockResolvedValue([])
  api.fetchMenus.mockReset().mockResolvedValue([])
  api.fetchDataScope.mockReset().mockResolvedValue([])
  api.setToken.mockReset()
  api.clearToken.mockReset()
}

beforeAll(() => {
  /** 创建唯一 Store，确保全局 auth-expired 监听始终指向当前测试实例。 */
  setActivePinia(createPinia())
  store = useUserStore()
})

beforeEach(resetUserStore)

describe('user store authentication and RBAC', () => {
  it('logs in, persists the token and loads merged RBAC state', async () => {
    /** 验证登录主流程与 project 数据范围去重合并。 */
    api.authLogin.mockResolvedValue({
      access_token: 'token-7',
      token_type: 'bearer',
      expires_in: 3600,
      user: member,
    })
    api.fetchRoles.mockResolvedValue([{ id: 2, code: 'reviewer', name: '审查员', is_builtin: true }])
    // 新契约:permissions 接口返回权限编码字符串数组(List[str]),非对象数组
    api.fetchPermissions.mockResolvedValue(['project:view'])
    api.fetchMenus.mockResolvedValue([
      {
        id: 4,
        parent_id: null,
        code: 'dashboard',
        name: '工作台',
        path: '/dashboard',
        sort_order: 1,
        is_visible: true,
      },
    ])
    // 新契约:data-scope 接口由后端聚合多角色后返回单个对象
    api.fetchDataScope.mockResolvedValue({ id: 5, role_id: 2, scope_type: 'custom', project_ids: [1, 2] })

    await store.login({ username: 'alice', password: 'secret' })

    expect(store.token).toBe('token-7')
    expect(store.profile).toEqual(member)
    expect(store.isLoggedIn).toBe(true)
    expect(store.displayName).toBe('Alice')
    expect(api.setToken).toHaveBeenCalledWith('token-7')
    expect(store.roles).toEqual(['reviewer'])
    expect([...store.permissions]).toEqual(['project:view'])
    expect(store.menus[0].path).toBe('/dashboard')
    expect(store.dataScope?.scope_type).toBe('custom')
    expect(store.dataScope?.project_ids).toEqual([1, 2])
  })

  it('keeps successful RBAC slices while failed slices degrade to empty state', async () => {
    /** 验证四项 RBAC 并发加载互不连坐。 */
    store.profile = member
    api.fetchRoles.mockRejectedValue(new Error('roles down'))
    api.fetchPermissions.mockResolvedValue(['agent:view'])
    api.fetchMenus.mockRejectedValue(new Error('menus down'))
    api.fetchDataScope.mockRejectedValue(new Error('scope down'))

    await store.loadRbacInfo()

    expect(store.roles).toEqual([])
    expect([...store.permissions]).toEqual(['agent:view'])
    expect(store.menus).toEqual([])
    expect(store.dataScope).toBeNull()
  })

  it('short-circuits RBAC calls without a profile', async () => {
    /** 验证空会话不访问任何用户维度 API。 */
    await store.fetchUserRoles()
    await store.fetchUserPermissions()
    await store.fetchUserMenus()
    await store.fetchDataScope()
    await store.loadRbacInfo()

    expect(api.fetchRoles).not.toHaveBeenCalled()
    expect(api.fetchPermissions).not.toHaveBeenCalled()
    expect(api.fetchMenus).not.toHaveBeenCalled()
    expect(api.fetchDataScope).not.toHaveBeenCalled()
  })

  it('adopts the backend-aggregated data scope object and handles failure', async () => {
    /** 验证直接采用后端聚合的单对象,接口失败时置 null。 */
    store.profile = member
    const allScope = { id: 9, role_id: 1, scope_type: 'all' as const }
    api.fetchDataScope.mockResolvedValue(allScope)

    await store.fetchDataScope()
    expect(store.dataScope).toEqual(allScope)

    // 接口失败/异常时 dataScope 置 null(后端 data-scope 恒返回单对象,不再返回空数组)
    api.fetchDataScope.mockRejectedValue(new Error('scope down'))
    await store.fetchDataScope()
    expect(store.dataScope).toBeNull()
  })

  it('checks roles, permissions and legacy admin compatibility', () => {
    /** 验证普通权限、RBAC admin 与历史 profile.role 三种判定。 */
    store.profile = { ...member, nickname: undefined }
    store.roles = ['reviewer']
    store.permissions = new Set(['review:start'])

    expect(store.displayName).toBe('alice')
    expect(store.hasRole('reviewer')).toBe(true)
    expect(store.hasPermission('review:start')).toBe(true)
    expect(store.hasPermission('review:delete')).toBe(false)
    expect(store.isAdmin()).toBe(false)

    store.roles = ['admin']
    expect(store.isAdmin()).toBe(true)
    expect(store.hasPermission('anything')).toBe(true)

    store.roles = []
    store.profile = { ...member, role: 'admin' }
    expect(store.isAdmin()).toBe(true)
  })

  it('registers and restores a profile before loading RBAC data', async () => {
    /** 验证注册代理和已有 token 的会话恢复。 */
    api.authRegister.mockResolvedValue({ user_id: 7, username: 'alice' })
    api.authMe.mockResolvedValue(member)
    api.fetchRoles.mockResolvedValue([{ id: 1, code: 'user', name: '用户', is_builtin: true }])

    await store.register({ username: 'alice', password: 'secret' })
    await store.fetchProfile()

    expect(api.authRegister).toHaveBeenCalledWith({ username: 'alice', password: 'secret' })
    expect(api.authMe).toHaveBeenCalledOnce()
    expect(store.profile).toEqual(member)
    expect(store.roles).toEqual(['user'])
  })

  it('logout and auth-expired events clear all local authorization state', () => {
    /** 验证主动退出与拦截器事件均清空敏感状态。 */
    store.$patch({
      token: 'token',
      profile: member,
      permissions: new Set(['project:view']),
      roles: ['reviewer'],
      menus: [{ id: 1 } as any],
      dataScope: { id: 1, role_id: 1, scope_type: 'all' },
    })

    window.dispatchEvent(new Event('prism:auth-expired'))
    expect(store.token).toBe('')
    expect(store.profile).toBeNull()
    expect(store.roles).toEqual([])
    expect([...store.permissions]).toEqual([])
    expect(api.clearToken).not.toHaveBeenCalled()

    store.$patch({ token: 'again', profile: member, roles: ['user'] })
    store.logout()
    expect(store.token).toBe('')
    expect(store.profile).toBeNull()
    expect(store.roles).toEqual([])
    expect(api.clearToken).toHaveBeenCalledOnce()
  })
})
