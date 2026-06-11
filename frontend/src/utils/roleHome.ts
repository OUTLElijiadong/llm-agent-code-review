export type UserRole = 'admin' | 'reviewer' | 'user'

const FALLBACK_ROLE: UserRole = 'user'

const ROLE_HOME_PATHS: Record<UserRole, string> = {
  admin: '/admin/users',
  reviewer: '/reviews',
  user: '/projects',
}

/**
 * 将后端返回的角色字符串归一化为前端已知角色
 * @param role - 后端用户角色
 * @returns 归一化后的用户角色
 */
export function normalizeRole(role?: string | null): UserRole {
  if (role === 'admin' || role === 'reviewer' || role === 'user') {
    return role
  }
  return FALLBACK_ROLE
}

/**
 * 获取指定角色登录后的默认首页
 * @param role - 后端用户角色
 * @returns 角色对应的默认页面路径
 */
export function getRoleHomePath(role?: string | null): string {
  return ROLE_HOME_PATHS[normalizeRole(role)]
}

/**
 * 判断指定角色是否允许直接进入某个路径
 * @param role - 后端用户角色
 * @param path - 目标路由路径
 * @returns 是否允许进入
 */
export function canRoleOpenPath(role: string | null | undefined, path: string): boolean {
  const normalizedRole = normalizeRole(role)
  if (!path.startsWith('/')) return false
  if (path.startsWith('/admin')) return normalizedRole === 'admin'
  if (path === '/login' || path === '/register') return false
  return true
}

/**
 * 解析登录成功后的跳转地址，优先保证角色进入自己的工作界面
 * @param role - 后端用户角色
 * @param redirect - 登录前记录的重定向路径
 * @returns 登录成功后应进入的路径
 */
export function resolvePostLoginPath(role: string | null | undefined, redirect?: string | null): string {
  const homePath = getRoleHomePath(role)
  const normalizedRole = normalizeRole(role)
  if (!redirect || redirect === '/' || redirect === '/dashboard') return homePath
  if (redirect.startsWith('/login') || redirect.startsWith('/register')) return homePath
  if (!canRoleOpenPath(normalizedRole, redirect)) return homePath
  return redirect
}
