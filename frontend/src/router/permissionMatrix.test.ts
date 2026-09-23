import { createPinia, setActivePinia } from 'pinia'
import type { RouteLocationNormalized, Router } from 'vue-router'
import { beforeEach, describe, expect, it } from 'vitest'

import { useUserStore } from '@/stores/user'
import { setupGuards } from './guards'
import router from './index'

const records = router.getRoutes().filter((record) => record.name && !record.meta.public)
const allMemberPermissions = records.flatMap((record) => record.meta.permissions || [])
type Guard = (to: RouteLocationNormalized, from: RouteLocationNormalized) => Promise<unknown>

function installedGuard(): Guard {
  let before!: Guard
  setupGuards({ beforeEach: (guard: Guard) => { before = guard }, afterEach: () => undefined } as unknown as Router)
  return before
}

function resolve(path: string): RouteLocationNormalized {
  return router.resolve(path.replace(/:[A-Za-z]+/g, '1')) as unknown as RouteLocationNormalized
}

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
})

describe('全部已注册私有页面的真实路由守卫与用户 store 矩阵', () => {
  it.each(records)('未登录访问 $path 保留原地址并跳转登录', async (record) => {
    const target = resolve(record.path)
    expect(await installedGuard()(target, resolve('/login'))).toEqual({
      path: '/login', query: { redirect: target.fullPath },
    })
  })

  it.each(records)('无权限账号访问 $path 按页面真实契约拒绝或开放', async (record) => {
    const user = useUserStore()
    user.token = 'isolated-frontend-fixture'
    user.profile = { id: 104, username: 'no_permission', role: 'user', status: 1 }
    user.roles = ['user']
    const target = resolve(record.path)
    const restricted = target.meta.superAdmin || target.meta.role || target.meta.roles?.some((role) => role !== 'user')
      || (target.meta.permissions?.length || 0) > 0
    expect(await installedGuard()(target, resolve('/dashboard'))).toEqual(restricted ? { path: '/403' } : true)
  })

  it.each(records)('普通成员访问 $path：成员业务正向放行、管理页面拒绝', async (record) => {
    const user = useUserStore()
    user.token = 'isolated-frontend-fixture'
    user.profile = { id: 102, username: 'member_a', role: 'user', status: 1 }
    user.roles = ['user']
    user.permissions = new Set(allMemberPermissions)
    const target = resolve(record.path)
    // 工坊等仅审查者页面对普通成员同样是拒绝面(meta.roles 不含 user)
    const denied = target.path.startsWith('/admin/') || target.meta.superAdmin
      || Boolean(target.meta.roles?.length && !target.meta.roles.includes('user'))
    expect(await installedGuard()(target, resolve('/dashboard'))).toEqual(denied ? { path: '/403' } : true)
  })

  it.each(records)('普通管理员访问 $path 仍受超级管理员限制', async (record) => {
    const user = useUserStore()
    user.token = 'isolated-frontend-fixture'
    user.profile = { id: 105, username: 'manager', role: 'admin', status: 1 }
    user.roles = ['admin']
    const target = resolve(record.path)
    expect(await installedGuard()(target, resolve('/admin/overview'))).toEqual(target.meta.superAdmin ? { path: '/403' } : true)
  })

  it.each(records)('超级管理员访问 $path 正向放行', async (record) => {
    const user = useUserStore()
    user.token = 'isolated-frontend-fixture'
    user.profile = { id: 106, username: 'admin', role: 'super_admin', status: 1 }
    user.roles = ['super_admin']
    expect(await installedGuard()(resolve(record.path), resolve('/admin/overview'))).toBe(true)
  })
})

describe('审查员与审计员合并后的操作审计权限', () => {
  it('拥有 audit:view 的审查员可进入，缺少该权限时拒绝', async () => {
    const user = useUserStore()
    user.token = 'isolated-reviewer-fixture'
    user.profile = { id: 107, username: 'reviewer', role: 'reviewer', status: 1 }
    user.roles = ['reviewer']
    user.permissions = new Set(['audit:view'])
    const target = resolve('/audit')

    expect(await installedGuard()(target, resolve('/dashboard'))).toBe(true)

    user.permissions = new Set(['review:view'])
    expect(await installedGuard()(target, resolve('/dashboard'))).toEqual({ path: '/403' })
  })
})
