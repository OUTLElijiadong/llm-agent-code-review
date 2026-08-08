/**
 * 路由守卫模块
 * 提供全局路由鉴权逻辑:登录验证、角色权限检查、RBAC 权限点检查
 */

import type { Router, RouteLocationNormalized, RouteRecordRaw } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { getRoleHomePath } from '@/utils/roleHome'

/**
 * 路由 meta 扩展字段,用于 RBAC 鉴权
 */
declare module 'vue-router' {
  interface RouteMeta {
    /** 页面标题 */
    title?: string
    /** 是否为公开页面(无需登录) */
    public?: boolean
    /** 历史角色字段(单值),与 profile.role 比对 */
    role?: string
    /** 需要的角色列表(满足任一即可,admin 自动放行) */
    roles?: string[]
    /** 需要的权限点列表(满足任一即可,admin 自动放行) */
    permissions?: string[]
    /** 是否仅允许唯一超级管理员 admin,此限制不可被普通管理员绕过 */
    superAdmin?: boolean
  }
}

/**
 * 安装全局路由守卫
 * @param router - Vue Router 实例
 */
export function setupGuards(router: Router): void {
  router.beforeEach(async (to: RouteLocationNormalized, _from: RouteLocationNormalized) => {
    const user = useUserStore()

    if (to.meta.public) {
      if ((to.path === '/login' || to.path === '/register') && user.token) {
        try {
          if (!user.profile) {
            await user.fetchProfile()
          }
          return { path: getRoleHomePath(user.profile?.role), replace: true }
        } catch {
          user.clearSession()
        }
      }
      return true
    }

    if (!user.token) {
      return { path: '/login', query: { redirect: to.fullPath } }
    }

    if (!user.profile) {
      try {
        await user.fetchProfile()
      } catch {
        user.clearSession()
        return { path: '/login', query: { redirect: to.fullPath } }
      }
    }

    if (to.path === '/') {
      return { path: getRoleHomePath(user.profile?.role), replace: true }
    }

    // 管理员只做管理内容工作:用户端页面仅保留管理后台跳转所需的"详情查看"路径
    // (项目/任务/代码文件/报告详情),其余用户端页面(沙箱、工作台、论坛等)一律回管理总览。
    if (user.isAdmin() && !to.path.startsWith('/admin') && to.path !== '/') {
      const adminViewable = /^\/(projects\/\d+|reviews\/\d+|code\/\d+\/file\/\d+|reports\/\d+)(\/|$)/.test(to.path)
      if (!adminViewable) {
        return { path: '/admin/overview', replace: true }
      }
    }

    // 唯一超级管理员限制必须先于 admin 的通用 RBAC 放行。
    if (to.meta.superAdmin && !user.isSuperAdmin()) {
      return { path: '/403' }
    }

    // admin 角色绕过后续普通 RBAC 检查
    const isAdmin = user.isAdmin()

    // 历史单角色字段检查(向后兼容)
    if (!isAdmin && to.meta.role && to.meta.role !== user.profile?.role) {
      return { path: '/403' }
    }

    // RBAC 角色列表检查:meta.roles 非空时,用户需拥有其中任一角色
    if (!isAdmin && to.meta.roles && to.meta.roles.length > 0) {
      const hasAnyRole = to.meta.roles.some((r) => user.hasRole(r))
      if (!hasAnyRole) {
        return { path: '/403' }
      }
    }

    // RBAC 权限点检查:meta.permissions 非空时,用户需拥有其中任一权限点
    if (!isAdmin && to.meta.permissions && to.meta.permissions.length > 0) {
      const hasAnyPerm = to.meta.permissions.some((p) => user.hasPermission(p))
      if (!hasAnyPerm) {
        return { path: '/403' }
      }
    }

    return true
  })

  router.afterEach((to: RouteLocationNormalized) => {
    const title = to.meta.title as string | undefined
    if (title) {
      document.title = `${title} - 棱镜 Prism`
    } else {
      document.title = '棱镜 Prism'
    }
  })
}

/**
 * 重新导出路由记录原始类型,便于其他模块引用
 */
export type { RouteRecordRaw }
