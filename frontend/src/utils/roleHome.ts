export type UserRole = 'admin' | 'reviewer' | 'user'

const FALLBACK_ROLE: UserRole = 'user'

const ROLE_HOME_PATHS: Record<UserRole, string> = {
  admin: '/admin/overview',
  reviewer: '/dashboard',
  user: '/dashboard',
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

// 管理员只允许进入「管理相关」页面：管理区(/admin/*)、平台工作台、个人账户，
// 以及管理员有管理职责的共享页（论坛版务 / 维修工单受理 / 反馈回复 / 平台安全与
// Agent 监控）。开发/用户专属页（项目、代码、审查、报告、知识库、个性化画像等）
// 对管理员隐藏且不可直达。
const ADMIN_ALLOWED_PREFIXES = [
  '/dashboard', '/agents', '/security', '/forum', '/support', '/admin',
]

/**
 * 判断管理员是否允许进入某路径（菜单可见性与路由守卫共用此唯一判定）
 * @param path - 目标路由路径
 * @returns 管理员是否允许进入
 */
export function canAdminOpenPath(path: string): boolean {
  // 个性化画像属于用户专属，管理员不可进；其余个人中心子页（改密/API配置）允许
  if (path === '/profile/personalization') return false
  if (path === '/profile' || path.startsWith('/profile/')) return true
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
 */
export function resolvePostLoginPath(role: string | null | undefined, redirect?: string | null): string {
  const homePath = getRoleHomePath(role)
  const normalizedRole = normalizeRole(role)
  if (!redirect || redirect === '/' || redirect === '/dashboard') return homePath
  if (redirect.startsWith('/login') || redirect.startsWith('/register')) return homePath
  if (!canRoleOpenPath(normalizedRole, redirect)) return homePath
  return redirect
}
