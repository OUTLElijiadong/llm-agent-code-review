import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const harness = vi.hoisted(() => ({
  push: vi.fn(),
  permission: true,
  role: 'user',
}))

vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/dashboard' }),
  useRouter: () => ({
    push: harness.push,
    resolve: ({ path }: { path: string }) => ({
      matched: [{}],
      meta: ['/agent-studio', '/projects'].includes(path) ? { permissions: ['test:permission'] } : {},
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
    hasPermission: () => harness.permission,
  }),
}))

import AppSidebar from './AppSidebar.vue'

describe('AppSidebar ordinary member navigation', () => {
  beforeEach(() => {
    window.localStorage.clear()
    harness.push.mockClear()
    harness.permission = true
    harness.role = 'user'
  })

  function mountSidebar() {
    return mount(AppSidebar, {
      global: {
        stubs: {
          'el-icon': true,
          'el-tooltip': { template: '<div><slot /></div>' },
        },
      },
    })
  }

  it('shows the private Agent Studio entry to an ordinary member', () => {
    const wrapper = mountSidebar()

    expect(wrapper.text()).toContain('Agent 工坊')
  })

  it('hides Agent Studio when the ordinary member lacks its route permission', () => {
    harness.permission = false
    const wrapper = mountSidebar()

    expect(wrapper.text()).not.toContain('Agent 工坊')
    expect(wrapper.find('[data-route="/agent-studio"]').exists()).toBe(false)
  })

  it('审查员有项目查看权限时显示项目入口', () => {
    harness.role = 'reviewer'
    const wrapper = mountSidebar()

    expect(wrapper.find('[data-route="/projects"]').exists()).toBe(true)
  })

  it('shows v3.6 and persists the collapsed island state', async () => {
    const wrapper = mountSidebar()
    expect(wrapper.text()).toContain('v3.6 · PRISM')

    await wrapper.get('.sidebar-toggle').trigger('click')

    expect(wrapper.classes()).toContain('is-collapsed')
    expect(window.localStorage.getItem('prism.sidebar.collapsed')).toBe('1')
    expect(wrapper.findAll('.nav-group-toggle').length).toBeGreaterThan(0)
  })
})
