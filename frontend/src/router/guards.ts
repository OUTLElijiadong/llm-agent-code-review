/**
 * 路由守卫模块
 * 提供全局路由鉴权逻辑：登录验证、角色权限检查
 */

import type { Router, RouteLocationNormalized } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { getRoleHomePath } from '@/utils/roleHome'

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

    if (to.meta.role && to.meta.role !== user.profile?.role) {
      return { path: '/403' }
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
