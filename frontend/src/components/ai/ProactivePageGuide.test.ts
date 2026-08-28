import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const auth = vi.hoisted(() => ({
  token: 'token',
  profile: { id: 7, role: 'user' },
  permission: true,
  agentPermission: true,
  admin: false,
  superAdmin: false,
}))

vi.mock('@/stores/user', () => ({
  useUserStore: () => ({
    token: auth.token,
    profile: auth.profile,
    isAdmin: () => auth.admin,
    isSuperAdmin: () => auth.superAdmin,
    hasRole: (role: string) => role === 'user',
    hasPermission: (code: string) => code === 'agent:chat' ? auth.agentPermission : auth.permission,
  }),
}))

import ProactivePageGuide from './ProactivePageGuide.vue'

async function mountGuide(path: string, surface: 'user' | 'admin'): Promise<{ wrapper: ReturnType<typeof mount>; router: Router }> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/projects', component: { template: '<div>projects</div>' }, meta: { permissions: ['project:view'] } },
      { path: '/admin/overview', component: { template: '<div>overview</div>' }, meta: { role: 'admin' } },
      { path: '/admin/mcp-workers', component: { template: '<div>mcp</div>' }, meta: { role: 'admin', superAdmin: true } },
      { path: '/login', component: { template: '<div>login</div>' } },
      { path: '/:pathMatch(.*)*', component: { template: '<div>fallback</div>' } },
    ],
  })
  router.push(path)
  await router.isReady()
  const wrapper = mount(ProactivePageGuide, {
    props: { surface },
    global: { plugins: [router] },
  })
  await flushPromises()
  return { wrapper, router }
}

describe('ProactivePageGuide', () => {
  beforeEach(() => {
    sessionStorage.clear()
    auth.token = 'token'
    auth.profile = { id: 7, role: 'user' }
    auth.permission = true
    auth.agentPermission = true
    auth.admin = false
    auth.superAdmin = false
  })
  afterEach(() => { sessionStorage.clear(); vi.restoreAllMocks() })

  it('进入有建议的页面时弹出引导', async () => {
    const { wrapper } = await mountGuide('/projects', 'user')
    expect(wrapper.find('.proactive-guide').exists()).toBe(true)
    expect(wrapper.text()).toContain('下一步建议')
  })

  it('点击引导派发用户端唤起事件并预填指令', async () => {
    const listener = vi.fn()
    window.addEventListener('prism:open-agent-chat', listener as EventListener)
    const { wrapper } = await mountGuide('/projects', 'user')
    await wrapper.find('.guide-act').trigger('click')
    expect(listener).toHaveBeenCalled()
    const event = listener.mock.calls[0][0] as CustomEvent<{ prefill: string }>
    expect(event.detail.prefill).toContain('项目管理')
    window.removeEventListener('prism:open-agent-chat', listener as EventListener)
  })

  it('切到已看过的页面时清空上一个页面的残留引导', async () => {
    const first = await mountGuide('/projects', 'user')
    expect(first.wrapper.find('.proactive-guide').exists()).toBe(true)
    first.wrapper.unmount()

    // 第二次进入 /projects 会被 sessionStorage 拦截;再切到 /login(无建议)后不应残留。
    const second = await mountGuide('/projects', 'user')
    expect(second.wrapper.find('.proactive-guide').exists()).toBe(false)
    await second.router.push('/login')
    await flushPromises()
    expect(second.wrapper.find('.proactive-guide').exists()).toBe(false)
    second.wrapper.unmount()
  })
  it('无建议的页面不弹出引导', async () => {
    const { wrapper } = await mountGuide('/login', 'user')
    expect(wrapper.find('.proactive-guide').exists()).toBe(false)
  })

  it('无路由权限时不显示主动引导', async () => {
    auth.permission = false
    const { wrapper } = await mountGuide('/projects', 'user')
    expect(wrapper.find('.proactive-guide').exists()).toBe(false)
    expect(sessionStorage.getItem('prism-page-guide:user:user-7')).toBeNull()
  })

  it('无 agent:chat 权限时不显示主动引导', async () => {
    auth.agentPermission = false
    const { wrapper } = await mountGuide('/projects', 'user')
    expect(wrapper.find('.proactive-guide').exists()).toBe(false)
    expect(sessionStorage.getItem('prism-page-guide:user:user-7')).toBeNull()
  })

  it('非超级管理员不显示超级管理员专属引导', async () => {
    auth.admin = true
    const { wrapper } = await mountGuide('/admin/mcp-workers', 'admin')
    expect(wrapper.find('.proactive-guide').exists()).toBe(false)
    expect(sessionStorage.getItem('prism-page-guide:admin:user-7')).toBeNull()
  })

  it('同一页面在当前会话只弹一次', async () => {
    const first = await mountGuide('/projects', 'user')
    expect(first.wrapper.find('.proactive-guide').exists()).toBe(true)
    first.wrapper.unmount()
    const second = await mountGuide('/projects', 'user')
    expect(second.wrapper.find('.proactive-guide').exists()).toBe(false)
  })

  it('主动引导已读状态按账号隔离', async () => {
    const first = await mountGuide('/projects', 'user')
    expect(first.wrapper.find('.proactive-guide').exists()).toBe(true)
    first.wrapper.unmount()

    auth.profile = { id: 8, role: 'user' }
    const second = await mountGuide('/projects', 'user')
    expect(second.wrapper.find('.proactive-guide').exists()).toBe(true)
  })
})
