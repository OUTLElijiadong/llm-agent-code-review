import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { UserOut, LoginIn, RegisterIn } from '@/types/auth'
import type { Menu, DataScope } from '@/types/rbac'
import {
  login as authLogin,
  logout as authLogout,
  register as authRegister,
  me as authMe,
} from '@/api/auth'
import {
  fetchUserRoles as apiFetchUserRoles,
  fetchUserPermissions as apiFetchUserPermissions,
  fetchUserMenus as apiFetchUserMenus,
  fetchUserDataScope as apiFetchUserDataScope,
} from '@/api/rbac'
import { setToken, clearToken, getToken } from '@/utils/token'

let authExpiredListenerRegistered = false

/**
 * 用户状态管理 Store,管理认证状态、用户信息、RBAC 权限与 Token 持久化
 */
export const useUserStore = defineStore('user', () => {
  const token = ref<string>(getToken() || '')
  const profile = ref<UserOut | null>(null)

  /** 用户权限点编码集合 */
  const permissions = ref<Set<string>>(new Set())
  /** 用户角色 code 列表 */
  const roles = ref<string[]>([])
  /** 用户可见菜单树 */
  const menus = ref<Menu[]>([])
  /** 用户数据范围(多角色取最宽范围) */
  const dataScope = ref<DataScope | null>(null)

  const isLoggedIn = computed(() => !!token.value && !!profile.value)
  const displayName = computed(() => profile.value?.nickname || profile.value?.username || '')

  /**
   * 拉取用户角色并填充 roles 状态
   * @returns void
   */
  async function fetchUserRoles(): Promise<void> {
    if (!profile.value) return
    try {
      const list = await apiFetchUserRoles(profile.value.id)
      roles.value = list.map((r) => r.code)
    } catch {
      roles.value = []
    }
  }

  /**
   * 拉取用户权限点并填充 permissions 状态
   * @returns void
   */
  async function fetchUserPermissions(): Promise<void> {
    if (!profile.value) return
    try {
      const list = await apiFetchUserPermissions(profile.value.id)
      // 后端返回权限编码字符串数组(List[str]),直接入 Set
      permissions.value = new Set(list)
    } catch {
      permissions.value = new Set()
    }
  }

  /**
   * 拉取用户菜单并填充 menus 状态
   * @returns void
   */
  async function fetchUserMenus(): Promise<void> {
    if (!profile.value) return
    try {
      menus.value = await apiFetchUserMenus(profile.value.id)
    } catch {
      menus.value = []
    }
  }

  /**
   * 拉取用户数据范围并填充 dataScope 状态
   * 后端已聚合多角色数据范围(取最宽范围 all > project > self),返回单个对象
   * @returns void
   */
  async function fetchDataScope(): Promise<void> {
    if (!profile.value) return
    try {
      const scope = await apiFetchUserDataScope(profile.value.id)
      dataScope.value = scope ?? null
    } catch {
      dataScope.value = null
    }
  }

  /**
   * 加载全部 RBAC 权限信息(角色/权限点/菜单/数据范围)
   * 任意子项失败不影响其他项,保证登录主流程不被阻断
   * @returns void
   */
  async function loadRbacInfo(): Promise<void> {
    if (!profile.value) return
    await Promise.all([
      fetchUserRoles(),
      fetchUserPermissions(),
      fetchUserMenus(),
      fetchDataScope(),
    ])
  }

  /**
   * 判断是否拥有某权限点
   * admin 角色始终返回 true,绕过所有权限检查
   * @param code - 权限点编码,例如 project:view
   * @returns 是否拥有该权限
   */
  function hasPermission(code: string): boolean {
    if (code.startsWith('server_ops:')) return isSuperAdmin()
    if (isAdmin()) return true
    return permissions.value.has(code)
  }

  /**
   * 判断是否拥有某角色
   * @param roleCode - 角色编码,例如 admin/reviewer/user
   * @returns 是否拥有该角色
   */
  function hasRole(roleCode: string): boolean {
    return roles.value.includes(roleCode)
  }

  /**
   * 判断是否为 admin 角色
   * 同时检查 RBAC roles 数组与历史 profile.role 字段;
   * super_admin 与后端 _ADMIN_LEGACY_ROLES 保持一致,视为管理员
   * @returns 是否为管理员
   */
  function isAdmin(): boolean {
    if (roles.value.includes('admin') || roles.value.includes('super_admin')) return true
    const legacy = profile.value?.role
    return legacy === 'admin' || legacy === 'super_admin'
  }

  /** 唯一超级管理员必须同时满足 admin 用户名与 super_admin 角色。 */
  function isSuperAdmin(): boolean {
    if (profile.value?.username !== 'admin') return false
    return profile.value.role === 'super_admin' && roles.value.includes('super_admin')
  }

  /**
   * 用户登录,保存 token、获取用户信息并加载 RBAC 权限
   * @param data - 登录请求参数
   */
  async function login(data: LoginIn) {
    const res = await authLogin(data)
    token.value = res.access_token
    setToken(res.access_token)
    profile.value = res.user
    await loadRbacInfo()
  }

  /**
   * 用户注册
   * @param data - 注册请求参数
   */
  async function register(data: RegisterIn) {
    await authRegister(data)
  }

  /**
   * 获取当前用户信息,用于从已有 Token 恢复会话
   * 同时加载 RBAC 权限信息,保证刷新后权限校验生效
   */
  async function fetchProfile() {
    profile.value = await authMe()
    await loadRbacInfo()
  }

  /**
   * 清空 RBAC 相关状态
   * @returns void
   */
  function clearRbacState(): void {
    permissions.value = new Set()
    roles.value = []
    menus.value = []
    dataScope.value = null
  }

  /**
   * 退出登录,清除本地状态与 RBAC 权限
   * @returns void
   */
  function clearSession(): void {
    token.value = ''
    profile.value = null
    clearRbacState()
    clearToken()
  }

  /** 先通知服务端吊销会话，网络失败时也确保本地退出。 */
  async function logout(): Promise<void> {
    try {
      if (token.value) await authLogout()
    } finally {
      clearSession()
    }
  }

  /**
   * 同步认证过期后的本地状态
   * @returns void
   */
  function syncAuthExpiredState(): void {
    token.value = ''
    profile.value = null
    clearRbacState()
    clearToken()
  }

  /**
   * 注册全局认证过期监听,保证拦截器清 token 后 Pinia 状态同步
   * @returns void
   */
  function registerAuthExpiredListener(): void {
    if (authExpiredListenerRegistered) return
    window.addEventListener('prism:auth-expired', syncAuthExpiredState)
    authExpiredListenerRegistered = true
  }

  registerAuthExpiredListener()

  return {
    token,
    profile,
    permissions,
    roles,
    menus,
    dataScope,
    isLoggedIn,
    displayName,
    login,
    register,
    fetchProfile,
    fetchUserRoles,
    fetchUserPermissions,
    fetchUserMenus,
    fetchDataScope,
    loadRbacInfo,
    hasPermission,
    hasRole,
    isAdmin,
    isSuperAdmin,
    clearSession,
    logout,
  }
})
