import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

const harness = vi.hoisted(() => ({
  push: vi.fn(),
  permission: false,
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({
    resolve: ({ path }: { path: string }) => ({
      fullPath: path,
      matched: [{}],
      meta: { permissions: ['agent_asset:create'] },
    }),
    push: harness.push,
  }),
}))

vi.mock('@/stores/user', () => ({
  useUserStore: () => ({
    token: 'token',
    isAdmin: () => false,
    isSuperAdmin: () => false,
    hasRole: () => true,
    hasPermission: () => harness.permission,
  }),
}))

import AgentNavLink from './AgentNavLink.vue'

describe('AgentNavLink permissions', () => {
  it('does not render an unauthorized navigation target', () => {
    harness.permission = false
    const wrapper = mount(AgentNavLink, {
      props: { href: '/agent-studio', label: 'Agent 工坊' },
      global: { stubs: { 'el-icon': true } },
    })

    expect(wrapper.find('button').exists()).toBe(false)
    expect(wrapper.text()).toBe('')
  })

  it('renders the route when its permission is present', () => {
    harness.permission = true
    const wrapper = mount(AgentNavLink, {
      props: { href: '/agent-studio', label: 'Agent 工坊' },
      global: { stubs: { 'el-icon': true } },
    })

    expect(wrapper.get('button').text()).toContain('Agent 工坊')
  })
})
