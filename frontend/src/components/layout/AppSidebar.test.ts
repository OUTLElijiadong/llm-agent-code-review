import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const harness = vi.hoisted(() => ({
  push: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/dashboard' }),
  useRouter: () => ({ push: harness.push }),
}))

vi.mock('@/stores/user', () => ({
  useUserStore: () => ({
    profile: { id: 7, role: 'user' },
    isAdmin: () => false,
    isSuperAdmin: () => false,
  }),
}))

import AppSidebar from './AppSidebar.vue'

describe('AppSidebar ordinary member navigation', () => {
  beforeEach(() => {
    window.localStorage.clear()
    harness.push.mockClear()
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

  it('shows v3.6 and persists the collapsed island state', async () => {
    const wrapper = mountSidebar()
    expect(wrapper.text()).toContain('v3.6 · PRISM')

    await wrapper.get('.sidebar-toggle').trigger('click')

    expect(wrapper.classes()).toContain('is-collapsed')
    expect(window.localStorage.getItem('prism.sidebar.collapsed')).toBe('1')
    expect(wrapper.findAll('.nav-group-toggle').length).toBeGreaterThan(0)
  })
})
