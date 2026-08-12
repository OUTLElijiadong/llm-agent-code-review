import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

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
  it('shows the private Agent Studio entry to an ordinary member', () => {
    const wrapper = mount(AppSidebar, {
      global: {
        stubs: {
          'el-icon': true,
        },
      },
    })

    expect(wrapper.text()).toContain('Agent 工坊')
  })
})
