import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const harness = vi.hoisted(() => ({
  push: vi.fn(),
  permission: true,
  auditPermission: true,
  role: 'user',
}))

vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/dashboard' }),
  useRouter: () => ({
    push: harness.push,
    resolve: ({ path }: { path: string }) => ({
      matched: [{}],
      meta: path === '/agents'
        ? { permissions: ['agent:view', 'agent_asset:create'] }
        : path === '/audit'
          ? { roles: ['reviewer'], permissions: ['audit:view'] }
        : path === '/projects'
          ? { permissions: ['test:permission'] }
          : {},
    }),
  }),
}))

vi.mock('@/stores/user', () => ({
  useUserStore: () => ({
    get profile() { return { id: 7, role: harness.role } },
    token: 'token',
    isAdmin: () => false,
    isSuperAdmin: () => false,
    hasRole: (role: string) => role === harness.role,
    hasPermission: (code: string) => code === 'audit:view' ? harness.auditPermission : harness.permission,
  }),
}))

import AppSidebar from './AppSidebar.vue'
import { APP_DISPLAY_VERSION } from '@/constants/buildInfo'

describe('AppSidebar ordinary member navigation', () => {
  beforeEach(() => {
    window.localStorage.clear()
    harness.push.mockClear()
    harness.permission = true
    harness.auditPermission = true
    harness.role = 'user'
  })

  function mountSidebar(mobileOpen = false) {
    return mount(AppSidebar, {
      props: { mobileOpen },
      global: {
        stubs: {
          'el-icon': true,
          'el-tooltip': { template: '<div><slot /></div>' },
        },
      },
    })
  }

  it('审查员从统一 Agent 工作台进入 Agent 管理与个人草稿', () => {
    harness.role = 'reviewer'
    const wrapper = mountSidebar()

    expect(wrapper.find('[data-route="/agents"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Agent 工作台')
  })

  it('审查员有 audit:view 时显示操作审计入口，缺权时隐藏', () => {
    harness.role = 'reviewer'
    const wrapper = mountSidebar()
    expect(wrapper.find('[data-route="/audit"]').exists()).toBe(true)

    wrapper.unmount()
    harness.auditPermission = false
    const restricted = mountSidebar()
    expect(restricted.find('[data-route="/audit"]').exists()).toBe(false)
  })

  it('普通用户使用同一 Agent 工作台,不再显示重复的 Agent 工坊入口', () => {
    const wrapper = mountSidebar()

    expect(wrapper.find('[data-route="/agents"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Agent 工作台')
    expect(wrapper.find('[data-route="/agent-studio"]').exists()).toBe(false)
  })

  it('hides Agent Studio when the reviewer lacks its route permission', () => {
    harness.role = 'reviewer'
    harness.permission = false
    const wrapper = mountSidebar()

    expect(wrapper.text()).not.toContain('Agent 工作台')
    expect(wrapper.find('[data-route="/agent-studio"]').exists()).toBe(false)
  })

  it('审查员有项目查看权限时显示项目入口', () => {
    harness.role = 'reviewer'
    const wrapper = mountSidebar()

    expect(wrapper.find('[data-route="/projects"]').exists()).toBe(true)
  })

  it('shows the current release and persists the collapsed island state', async () => {
    const wrapper = mountSidebar()
    expect(wrapper.text()).toContain(`${APP_DISPLAY_VERSION} · PRISM`)

    await wrapper.get('.sidebar-toggle').trigger('click')

    expect(wrapper.classes()).toContain('is-collapsed')
    expect(window.localStorage.getItem('prism.sidebar.collapsed')).toBe('1')
    expect(wrapper.findAll('.nav-group-toggle').length).toBeGreaterThan(0)
  })

  it('桌面收起状态不妨碍手机抽屉折叠分组与关闭', async () => {
    window.localStorage.setItem('prism.sidebar.collapsed', '1')
    const wrapper = mountSidebar(true)
    const group = wrapper.get('.nav-group-toggle')
    const items = wrapper.get('.nav-group-items')

    expect(items.attributes('style') ?? '').not.toContain('display: none')
    await group.trigger('click')
    expect(group.attributes('aria-expanded')).toBe('false')
    expect(items.attributes('style')).toContain('display: none')

    await wrapper.get('button[aria-label="关闭侧边栏"]').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })
})
