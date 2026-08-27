export type UserRole = 'admin' | 'reviewer' | 'user'

const FALLBACK_ROLE: UserRole = 'user'

const ROLE_HOME_PATHS: Record<UserRole, string> = {
  admin: '/admin/overview',
  reviewer: '/dashboard',
  user: '/dashboard',
}

/**
 * 判断指定角色是否应显示某个已认证导航项
 * @param role - 后端用户角色
 * @param allowedRoles - 导航项声明的可见角色;为空表示全员可见
 * @returns 当前角色是否可见
 */
export function canRoleSeeNavigationItem(
  role: string | null | undefined,
  allowedRoles?: UserRole[],
): boolean {
  const normalizedRole = normalizeRole(role)
  if (normalizedRole === 'admin') return true
  return !allowedRoles || allowedRoles.includes(normalizedRole)
}

/**
 * 将后端返回的角色字符串归一化为前端已知角色
 * @param role - 后端用户角色
 * @returns 归一化后的用户角色
 */
export function normalizeRole(role?: string | null): UserRole {
  if (role === 'super_admin') return 'admin'
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

// 管理员作为超级用户，可同时使用主站用户功能与 Agent 治理后台。
// 仅允许已知站内路由前缀，避免把外部/未知路径当作登录后重定向目标。
const ADMIN_ALLOWED_PREFIXES = [
  '/dashboard', '/projects', '/code', '/reviews', '/issues', '/reports',
  '/agents', '/sandboxes', '/agent-studio', '/security', '/rules', '/forum', '/knowledge', '/support',
  '/profile', '/admin',
]

/**
 * 判断管理员是否允许进入某路径（菜单可见性与路由守卫共用此唯一判定）
 * @param path - 目标路由路径
 * @returns 管理员是否允许进入
 */
export function canAdminOpenPath(path: string): boolean {
  if (!path.startsWith('/') || path.startsWith('//')) return false
  if (path === '/login' || path === '/register') return false
  return ADMIN_ALLOWED_PREFIXES.some((p) => path === p || path.startsWith(p + '/'))
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
  // 拒绝协议相对跳转(//evil.com)与 /admin 路径的角色越权
  if (path.startsWith('//')) return false
  if (path.startsWith('/admin')) return normalizedRole === 'admin'
  if (path === '/login' || path === '/register') return false
  if (normalizedRole === 'admin') return canAdminOpenPath(path)
  return true
}

/**
 * 解析登录成功后的跳转地址，优先保证角色进入自己的工作界面
 * @param role - 后端用户角色
 * @param redirect - 登录前记录的重定向路径
 * @returns 登录成功后应进入的路径
 *
 * 管理员固定回到总览大屏:管理后台页面间跳转产生的 redirect 参数
 * (如超时回登录页)不应把重新登录后的落脚点带离总览;
 * 普通用户按 redirect 回原页面。
 */
export function resolvePostLoginPath(role: string | null | undefined, redirect?: string | null): string {
  const homePath = getRoleHomePath(role)
  const normalizedRole = normalizeRole(role)
  if (!redirect || redirect === '/' || redirect === '/dashboard') return homePath
  if (redirect.startsWith('/login') || redirect.startsWith('/register')) return homePath
  if (!canRoleOpenPath(normalizedRole, redirect)) return homePath
  // 管理员重新登录默认落总览大屏;仅当 redirect 明确指向另一个管理页时才跟随
  if (normalizedRole === 'admin' && !redirect.startsWith('/admin')) return homePath
  return redirect
}
