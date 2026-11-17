import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter, type RouteRecordRaw } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import { useUserStore } from '@/stores/user'
import { useWorkspaceStore } from '@/stores/workspace'

vi.mock('@/services/enterpriseMembership', () => ({
  hasEnterpriseMembership: vi.fn(async () => false),
}))

import AdminLayout from './AdminLayout.vue'
import source from './AdminLayout.vue?raw'

const styles = source.split('<style scoped lang="scss">')[1].split('</style>')[0]

describe('AdminLayout scrolling contract', () => {
  it('keeps sidebar and content in a fixed independent scroll shell', () => {
    expect(source).toContain('height: 100dvh')
    expect(source).toContain('overflow: hidden')
    expect(source).toContain('min-height: 0')
    expect(source).toContain('overflow-y: auto')
    expect(source).toContain('scrollTo')
    expect(source).toContain('overscroll-behavior: contain')
  })
})

describe('管理员底部操作区避让副驾入口', () => {
  it('主滚动区为60px入口、24px底距和16px间隔保留空间，并包含设备安全区', () => {
    const content = styles.match(/\.admin-content\s*\{([^}]+)\}/)?.[1] ?? ''
    expect(content).toContain('--assistant-action-clearance: calc(100px + env(safe-area-inset-bottom, 0px))')
    expect(content).toContain('padding-bottom: max(36px, var(--assistant-action-clearance))')
    expect(content).toContain('scroll-padding-bottom: var(--assistant-action-clearance)')
    expect(content).toContain('overflow-y: auto')
    expect(content).toContain('overflow-x: auto')
    expect(content).not.toContain('overflow-x: hidden')
  })

  it.each([920, 480])('宽度不超过%dpx时保持底部安全区，只调整顶边与横向间距', (width) => {
    const media = styles.split(`@media (max-width: ${width}px)`)[1]?.split('@media')[0] ?? ''
    const content = media.match(/\.admin-content\s*\{([^}]+)\}/)?.[1] ?? ''
    expect(content).toContain('padding-top:')
    expect(content).toContain('padding-inline:')
    expect(content).not.toMatch(/(?:^|;)\s*padding(?:-bottom)?\s*:/)
  })
})

describe('管理员手机端小菱入口', () => {
  it('入口有独立头部槽位，避免固定浮窗盖住运行卡片', () => {
    expect(source).toContain('id="admin-copilot-trigger-slot"')
    expect(styles).toContain('@media (max-width: 520px)')
    expect(styles).toMatch(/\.admin-copilot-trigger-slot\s*\{[^}]*min-height:\s*44px;/s)
    const mobile = styles.split('@media (max-width: 520px)')[1]?.split('@media')[0] ?? ''
    expect(mobile).toMatch(/\.admin-copilot-trigger-slot\s*\{[^}]*display:\s*grid;/s)
  })
})

it('手机端导航按完整分组横向滚动，避免分类标题被压缩', () => {
  const mobile = styles.split('@media (max-width: 920px)')[1]?.split('@media')[0] ?? ''
  expect(mobile).toMatch(/\.admin-nav\s*\{[^}]*overflow-x:\s*auto;/s)
  expect(mobile).toMatch(/\.admin-nav-group\s*\{[^}]*flex:\s*0 0 auto;/s)
  expect(mobile).toMatch(/\.admin-nav-items\s*\{[^}]*display:\s*flex;/s)
})

it('管理员布局按平台、计费、工作区和社区分组，计费入口不再藏在另一套侧栏里', async () => {
  const paths = [
    '/admin/governance', '/admin/operations', '/admin/security-center', '/admin/access', '/admin/platform',
    '/admin/billing-refunds', '/admin/billing-statements', '/admin/billing-reservations', '/admin/billing-reviews',
    '/admin/billing-rate-reviews', '/admin/billing-refund-reviews', '/admin/billing-pricing',
    '/admin/enterprise-asset-publications', '/usage', '/billing', '/enterprise', '/my-agents', '/agent-assets',
    '/remote-pentests',
    '/forum', '/support',
  ]
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      ...paths.map((path) => ({
        path,
        component: { template: '<main />' },
        meta: path === '/forum' ? { workspace: 'personal' as const } : {},
      } as RouteRecordRaw)),
      {
        path: '/forum/:pathMatch(.*)*',
        component: { template: '<main />' },
        meta: { workspace: 'personal' as const },
      },
      { path: '/admin/:pathMatch(.*)*', component: { template: '<main />' } },
    ],
  })
  await router.push('/admin/operations')
  const pinia = createPinia()
  const userStore = useUserStore(pinia)
  userStore.token = 'test-token'
  userStore.profile = { id: 1, username: 'admin', role: 'super_admin', status: 1 }
  const workspace = useWorkspaceStore(pinia)
  const wrapper = mount(AdminLayout, {
    global: {
      plugins: [pinia, router],
      stubs: {
        ProactivePageGuide: true,
        UserAvatar: true,
        'el-dropdown': { template: '<div><slot /></div>' },
        'el-dropdown-menu': true,
        'el-dropdown-item': true,
        'el-icon': { template: '<span><slot /></span>' },
      },
    },
  })

  try {
    await flushPromises()
    expect(wrapper.find('[data-route="/admin/governance"]').exists()).toBe(true)
    expect(wrapper.find('[data-route="/admin/security-center"]').exists()).toBe(true)
    expect(wrapper.find('[data-route="/forum"]').exists()).toBe(true)
    expect(wrapper.find('[data-route="/support"]').exists()).toBe(true)
    expect(wrapper.find('[data-route="/admin/billing-statements"]').exists()).toBe(true)
    expect(wrapper.find('[data-route="/billing"]').exists()).toBe(true)
    expect(wrapper.get('[data-route="/admin/platform"]').element.closest('.admin-nav-group')?.textContent)
      .toContain('平台治理')
    expect(wrapper.get('[data-route="/admin/billing-statements"]').element.closest('.admin-nav-group')?.textContent)
      .toContain('计费与对账')
    expect(wrapper.get('[data-route="/billing"]').element.closest('.admin-nav-group')?.textContent)
      .toContain('计费与对账')
    expect(wrapper.get('[data-route="/support"]').element.closest('.admin-nav-group')?.textContent)
      .toContain('社区与支持')

    await router.push('/FORUM/new')
    await flushPromises()
    const forumNavigation = wrapper.get('[data-route="/forum"]')
    expect(forumNavigation.attributes('aria-current')).toBe('page')
    expect(forumNavigation.element.closest('.admin-nav-group')
      ?.querySelector('.admin-nav-section')?.getAttribute('aria-expanded')).toBe('true')

    workspace.setEnterpriseMembership(true)
    await flushPromises()
    expect(workspace.activeSpace).toBe('personal')
    expect(wrapper.find('[data-route="/forum"]').exists()).toBe(false)

    workspace.setEnterpriseMembership(false)
    await flushPromises()
    expect(wrapper.find('[data-route="/forum"]').exists()).toBe(true)

    workspace.activateEnterprise(25)
    await flushPromises()
    expect(wrapper.find('[data-route="/forum"]').exists()).toBe(false)
    workspace.activatePersonal()
    await flushPromises()
    expect(wrapper.find('[data-route="/forum"]').exists()).toBe(true)
  } finally {
    wrapper.unmount()
  }
})

it('所有管理页都在页头预留小菱入口', async () => {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/admin/:pathMatch(.*)*', component: { template: '<main />' } }],
  })
  await router.push('/admin/security-center')
  const wrapper = mount(AdminLayout, {
    global: {
      plugins: [createPinia(), router],
      stubs: {
        ProactivePageGuide: true,
        UserAvatar: true,
        'el-dropdown': { template: '<div><slot /></div>' },
        'el-dropdown-menu': true,
        'el-dropdown-item': true,
        'el-icon': { template: '<span><slot /></span>' },
      },
    },
  })
  const content = wrapper.get<HTMLElement>('.admin-content').element
  content.scrollTo = vi.fn()
  try {
    const slot = wrapper.get('#admin-copilot-trigger-slot')
    expect(slot.element.closest('.admin-header')).not.toBeNull()
    expect(slot.classes()).toContain('is-page-header-trigger')

    await router.push('/admin/security-center?tab=events')
    await flushPromises()
    expect(slot.classes()).toContain('is-page-header-trigger')

    await router.push('/admin/operations')
    await flushPromises()
    expect(slot.classes()).toContain('is-page-header-trigger')

    await router.push('/admin/governance')
    await flushPromises()
    expect(slot.classes()).toContain('is-page-header-trigger')
  } finally {
    wrapper.unmount()
  }
})
