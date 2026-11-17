import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { defineComponent } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import { useUserStore } from '@/stores/user'
import { useWorkspaceStore } from '@/stores/workspace'
import AppLayout from './AppLayout.vue'
import AdminLayout from '@/components/admin/AdminLayout.vue'

vi.mock('@/services/enterpriseMembership', () => ({ hasEnterpriseMembership: vi.fn(async () => false) }))

const Page = defineComponent({
  template: '<div data-xiaoling-page="/support" :data-xiaoling-section="$route.query.section || \'maintenance\'">支持内容</div>',
})
const BillingPage = defineComponent({ template: '<div data-xiaoling-page="/billing">充值内容</div>' })
const WorkspacePage = defineComponent({ template: '<div>工作区内容</div>' })

async function mountRoute(role: 'admin' | 'super_admin' | 'user' | 'reviewer', path = '/support?section=feedback') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', component: AppLayout, children: [
      { path: 'support', component: Page, meta: { title: '支持中心' } },
      { path: 'billing', component: BillingPage, meta: { title: '充值与订阅' } },
      { path: 'enterprise', component: WorkspacePage, meta: { title: '企业空间' } },
    ] }, { path: '/admin', component: AdminLayout, children: [
      { path: 'platform', component: WorkspacePage, meta: { title: '平台配置', role: 'admin' } },
    ] }],
  })
  const pinia = createPinia()
  const user = useUserStore(pinia)
  user.token = 'unit-token'
  user.profile = { id: 1, username: 'test', role, status: 1 }
  await router.push(path)
  const wrapper = mount(defineComponent({ template: '<router-view />' }), {
    global: { plugins: [pinia, router], stubs: {
      AppSidebar: { template: '<aside class="user-sidebar" />' },
      AppHeader: { template: '<header class="user-header" />' },
      ProactivePageGuide: true, XiaolingGreeter: true, UserAvatar: true,
      'el-dropdown': { template: '<div><slot /></div>' },
      'el-dropdown-menu': true, 'el-dropdown-item': true,
      'el-icon': { template: '<span><slot /></span>' },
    } },
  })
  await flushPromises()
  if ((role === 'admin' || role === 'super_admin') && path.startsWith('/support')) {
    await vi.waitFor(() => expect(wrapper.find('.admin-layout').exists()).toBe(true))
  }
  return { wrapper, router, user, workspace: useWorkspaceStore(pinia) }
}

describe('支持中心按平台管理员选择外壳', () => {
  it.each(['admin', 'super_admin'] as const)('%s 使用管理外壳并保留支持页查询与上下文', async (role) => {
    const { wrapper, router } = await mountRoute(role)
    expect(wrapper.find('.admin-layout').exists()).toBe(true)
    expect(wrapper.find('.app-layout').exists()).toBe(false)
    expect(wrapper.get('[data-xiaoling-page="/support"]').attributes('data-xiaoling-section')).toBe('feedback')
    expect(router.currentRoute.value.fullPath).toBe('/support?section=feedback')
    expect(wrapper.get('[data-route="/support"]').attributes('aria-current')).toBe('page')
    expect(wrapper.findAll('#admin-copilot-trigger-slot')).toHaveLength(1)
    wrapper.unmount()
  })

  it.each(['user', 'reviewer'] as const)('%s 保持原有外壳和支持功能', async (role) => {
    const { wrapper, router } = await mountRoute(role)
    expect(wrapper.find('.app-layout').exists()).toBe(true)
    expect(wrapper.find('.admin-layout').exists()).toBe(false)
    expect(wrapper.get('[data-xiaoling-page="/support"]').attributes('data-xiaoling-section')).toBe('feedback')
    expect(router.currentRoute.value.fullPath).toBe('/support?section=feedback')
    wrapper.unmount()
  })

  it.each(['admin', 'super_admin'] as const)('%s 的充值页使用管理外壳并保留查询参数', async (role) => {
    const { wrapper, router } = await mountRoute(role, '/billing?enterprise_id=7')
    await vi.waitFor(() => expect(wrapper.find('.admin-layout').exists()).toBe(true))
    expect(wrapper.find('.app-layout').exists()).toBe(false)
    expect(wrapper.findAll('#admin-copilot-trigger-slot')).toHaveLength(1)
    expect(wrapper.get('[data-route="/billing"]').attributes('aria-current')).toBe('page')
    expect(wrapper.get('[data-xiaoling-page="/billing"]').text()).toBe('充值内容')
    expect(router.currentRoute.value.fullPath).toBe('/billing?enterprise_id=7')
    wrapper.unmount()
  })

  it.each(['user', 'reviewer'] as const)('%s 的充值页继续使用原外壳', async (role) => {
    const { wrapper } = await mountRoute(role, '/billing')
    expect(wrapper.find('.app-layout').exists()).toBe(true)
    expect(wrapper.find('.admin-layout').exists()).toBe(false)
    expect(wrapper.get('[data-xiaoling-page="/billing"]').text()).toBe('充值内容')
    wrapper.unmount()
  })

  it('管理员在支持和充值间往返时保留管理导航与唯一小菱入口', async () => {
    const { wrapper, router } = await mountRoute('admin')
    await wrapper.get('[data-route="/billing"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/billing')
    expect(wrapper.find('.admin-layout').exists()).toBe(true)
    expect(wrapper.findAll('#admin-copilot-trigger-slot')).toHaveLength(1)
    expect(wrapper.get('[data-route="/billing"]').attributes('aria-current')).toBe('page')
    await wrapper.get('[data-route="/support"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/support')
    expect(wrapper.find('.admin-layout').exists()).toBe(true)
    expect(wrapper.findAll('#admin-copilot-trigger-slot')).toHaveLength(1)
    wrapper.unmount()
  })

  it('管理员在平台配置和充值间往返时管理导航与小菱槽位可用', async () => {
    const { wrapper, router } = await mountRoute('admin', '/admin/platform')
    expect(wrapper.get('[data-route="/admin/platform"]').attributes('aria-current')).toBe('page')
    await wrapper.get('[data-route="/billing"]').trigger('click')
    await flushPromises()
    await vi.waitFor(() => expect(wrapper.get('[data-route="/billing"]').attributes('aria-current')).toBe('page'))
    expect(router.currentRoute.value.path).toBe('/billing')
    expect(wrapper.findAll('#admin-copilot-trigger-slot')).toHaveLength(1)
    await wrapper.get('[data-route="/admin/platform"]').trigger('click')
    await flushPromises()
    await vi.waitFor(() => expect(wrapper.get('[data-route="/admin/platform"]').attributes('aria-current')).toBe('page'))
    expect(router.currentRoute.value.path).toBe('/admin/platform')
    expect(wrapper.findAll('#admin-copilot-trigger-slot')).toHaveLength(1)
    wrapper.unmount()
  })

  it('管理员其他工作区页面仍使用原外壳', async () => {
    const { wrapper } = await mountRoute('admin', '/enterprise')
    expect(wrapper.find('.app-layout').exists()).toBe(true)
    expect(wrapper.find('.admin-layout').exists()).toBe(false)
    wrapper.unmount()
  })

  it('管理员支持页仍可从企业空间切回个人空间', async () => {
    const { wrapper, workspace } = await mountRoute('admin')
    workspace.activateEnterprise(7)
    await flushPromises()
    await wrapper.get('[data-workspace-switch]').trigger('click')
    expect(workspace.activeSpace).toBe('personal')
    expect(wrapper.find('.admin-layout').exists()).toBe(true)
    wrapper.unmount()
  })

  it('窄屏首次进入时将当前导航项滚入可视范围', async () => {
    const oldWidth = window.innerWidth
    const oldScroll = HTMLElement.prototype.scrollIntoView
    const reveal = vi.fn()
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 })
    HTMLElement.prototype.scrollIntoView = reveal
    try {
      const { wrapper, router, workspace } = await mountRoute('admin')
      expect(reveal).toHaveBeenCalledWith({ block: 'nearest', inline: 'nearest' })
      reveal.mockClear()
      await router.replace({ query: { section: 'maintenance', ticket: '42' } })
      await flushPromises()
      expect(reveal).not.toHaveBeenCalled()
      expect(router.currentRoute.value.query).toEqual({ section: 'maintenance', ticket: '42' })
      workspace.hasEnterpriseMembership = null
      await flushPromises()
      await wrapper.get('.admin-nav').trigger('pointerdown')
      workspace.hasEnterpriseMembership = false
      await flushPromises()
      expect(reveal).not.toHaveBeenCalled()
      wrapper.unmount()
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: oldWidth })
      HTMLElement.prototype.scrollIntoView = oldScroll
    }
  })

  it('管理员角色失效后恢复普通外壳并保持当前支持栏目', async () => {
    const { wrapper, user, router } = await mountRoute('admin')
    user.roles = ['admin']
    user.profile = { id: 1, username: 'test', role: 'user', status: 1 }
    user.roles = ['user']
    await flushPromises()
    expect(wrapper.find('.admin-layout').exists()).toBe(false)
    expect(wrapper.find('.app-layout').exists()).toBe(true)
    expect(wrapper.get('[data-xiaoling-page="/support"]').attributes('data-xiaoling-section')).toBe('feedback')
    expect(router.currentRoute.value.fullPath).toBe('/support?section=feedback')
    wrapper.unmount()
  })

  it('充值页管理员降权后恢复普通外壳并保留企业查询参数', async () => {
    const { wrapper, user, router } = await mountRoute('admin', '/billing?enterprise_id=7')
    await vi.waitFor(() => expect(wrapper.find('.admin-layout').exists()).toBe(true))
    user.roles = ['user']
    user.profile = { id: 1, username: 'test', role: 'user', status: 1 }
    await flushPromises()
    expect(wrapper.find('.admin-layout').exists()).toBe(false)
    expect(wrapper.find('.app-layout').exists()).toBe(true)
    expect(router.currentRoute.value.fullPath).toBe('/billing?enterprise_id=7')
    wrapper.unmount()
  })
})
