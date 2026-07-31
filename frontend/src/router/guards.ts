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
          user.logout()
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
        user.logout()
        return { path: '/login', query: { redirect: to.fullPath } }
      }
    }

    if (to.path === '/') {
      return { path: getRoleHomePath(user.profile?.role), replace: true }
    }

    // admin 角色绕过后续所有 RBAC 检查
    const isAdmin = user.isAdmin()

    // 历史单角色字段检查(向后兼容)
    if (to.meta.role && to.meta.role !== user.profile?.role) {
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
