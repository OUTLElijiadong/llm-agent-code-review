import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { useAgentActivityStore } from '@/stores/agentActivity'
import AgentNavLink from './AgentNavLink.vue'
import VirtualCursor from './VirtualCursor.vue'

beforeEach(() => {
  vi.useFakeTimers()
  setActivePinia(createPinia())
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation(callback => { callback(0); return 1 })
  vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => undefined)
})
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

it('真实导航链接在账号切换并移除源组件后，旧请求不能通过光标兜底跳转', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', component: {} }, { path: '/reports', component: {} },
  ] })
  await router.push('/')
  const push = vi.spyOn(router, 'push')
  const user = useUserStore()
  user.token = 'account-a-token'
  user.profile = { id: 1, username: 'account-a', role: 'user', status: 1 }
  const cursor = mount(VirtualCursor, { global: { stubs: { Transition: false } } })
  const link = mount(AgentNavLink, { props: { href: '/reports', label: '报告' }, global: { plugins: [router], stubs: { 'el-icon': true } } })
  try {
    await link.get('button').trigger('click')
    user.profile = { id: 2, username: 'account-b', role: 'user', status: 1 }
    user.token = 'account-b-token'
    link.unmount()
    useAgentActivityStore().clear()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(5000)
    expect(push).not.toHaveBeenCalled()
    expect(router.currentRoute.value.path).toBe('/')
  } finally { cursor.unmount() }
})

it.each(['session', 'permission', 'role', 'workspace', 'unmount'] as const)(
  '延迟导航期间%s变化即使恢复，也不能执行旧请求', async change => {
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/', component: {} }, { path: '/reports', component: {}, meta: { permissions: ['reports:view'] } },
    ] })
    await router.push('/')
    const push = vi.spyOn(router, 'push')
    const user = useUserStore()
    user.token = 'account-a-token'
    user.profile = { id: 1, username: 'account-a', role: 'user', status: 1 }
    user.permissions.add('reports:view')
    const cursor = mount(VirtualCursor, { global: { stubs: { Transition: false } } })
    const link = mount(AgentNavLink, { props: { href: '/reports', label: '报告', sessionId: 'one' }, global: { plugins: [router], stubs: { 'el-icon': true } } })
    try {
      await link.get('button').trigger('click')
      if (change === 'session') { await link.setProps({ sessionId: 'two' }); await link.setProps({ sessionId: 'one' }) }
      if (change === 'permission') { user.permissions.delete('reports:view'); user.permissions.add('reports:view') }
      if (change === 'role') { user.roles.push('reviewer'); user.roles.pop() }
      if (change === 'workspace') {
        const { useWorkspaceStore } = await import('@/stores/workspace')
        const workspace = useWorkspaceStore()
        workspace.activateEnterprise(23)
        workspace.activatePersonal()
      }
      if (change === 'unmount') link.unmount()
      useAgentActivityStore().clear()
      await flushPromises()
      await vi.advanceTimersByTimeAsync(5000)
      expect(push).not.toHaveBeenCalled()
    } finally { if (change !== 'unmount') link.unmount(); cursor.unmount() }
  },
)

it('身份、权限和会话未变化时，延迟兜底仍执行一次正常导航', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', component: {} }, { path: '/reports', component: {} },
  ] })
  await router.push('/')
  const push = vi.spyOn(router, 'push')
  const user = useUserStore()
  user.token = 'account-a-token'
  user.profile = { id: 1, username: 'account-a', role: 'user', status: 1 }
  const cursor = mount(VirtualCursor, { global: { stubs: { Transition: false } } })
  const link = mount(AgentNavLink, { props: { href: '/reports', label: '报告', sessionId: 'one' }, global: { plugins: [router], stubs: { 'el-icon': true } } })
  try {
    await link.get('button').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(5000)
    expect(push).toHaveBeenCalledExactlyOnceWith('/reports')
    expect(router.currentRoute.value.path).toBe('/reports')
  } finally { link.unmount(); cursor.unmount() }
})

it('中断或被替代时不执行旧请求，替代导航仍可完成', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', component: {} }, { path: '/reports', component: {} }, { path: '/audit', component: {} },
  ] })
  await router.push('/')
  const push = vi.spyOn(router, 'push')
  const user = useUserStore()
  user.token = 'account-a-token'
  user.profile = { id: 1, username: 'account-a', role: 'user', status: 1 }
  const cursor = mount(VirtualCursor, { global: { stubs: { Transition: false } } })
  const reports = mount(AgentNavLink, { props: { href: '/reports', label: '报告' }, global: { plugins: [router], stubs: { 'el-icon': true } } })
  const audit = mount(AgentNavLink, { props: { href: '/audit', label: '审计' }, global: { plugins: [router], stubs: { 'el-icon': true } } })
  try {
    await reports.get('button').trigger('click')
    await audit.get('button').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(5000)
    expect(push).toHaveBeenCalledExactlyOnceWith('/audit')
    expect(router.currentRoute.value.path).toBe('/audit')
  } finally { reports.unmount(); audit.unmount(); cursor.unmount() }
})

it('显式清除活动会取消旧导航', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', component: {} }, { path: '/reports', component: {} },
  ] })
  await router.push('/')
  const push = vi.spyOn(router, 'push')
  const user = useUserStore()
  user.token = 'account-a-token'
  user.profile = { id: 1, username: 'account-a', role: 'user', status: 1 }
  const cursor = mount(VirtualCursor, { global: { stubs: { Transition: false } } })
  const link = mount(AgentNavLink, { props: { href: '/reports', label: '报告' }, global: { plugins: [router], stubs: { 'el-icon': true } } })
  try {
    await link.get('button').trigger('click')
    useAgentActivityStore().clear()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(5000)
    expect(push).not.toHaveBeenCalled()
  } finally { link.unmount(); cursor.unmount() }
})

it('无虚拟光标的立即模式在事件派发期间账号变化时不执行旧请求', async () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', component: {} }, { path: '/reports', component: {} },
  ] })
  await router.push('/')
  const push = vi.spyOn(router, 'push')
  const user = useUserStore()
  user.token = 'account-a-token'
  user.profile = { id: 1, username: 'account-a', role: 'user', status: 1 }
  const listener = () => { user.profile = { id: 2, username: 'account-b', role: 'user', status: 1 } }
  window.addEventListener('prism:xiaoling-navigate', listener)
  const link = mount(AgentNavLink, { props: { href: '/reports', label: '报告' }, global: { plugins: [router], stubs: { 'el-icon': true } } })
  try {
    await link.get('button').trigger('click')
    await flushPromises()
    expect(push).not.toHaveBeenCalled()
  } finally { window.removeEventListener('prism:xiaoling-navigate', listener); link.unmount() }
})
