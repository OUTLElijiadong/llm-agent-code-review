import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

const harness = vi.hoisted(() => ({
  push: vi.fn(),
  permission: false,
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({
    resolve: (target: string | { path: string }) => {
      const raw = typeof target === 'string' ? target : target.path
      const fullPath = typeof target === 'string' ? raw : raw.split(/[?#]/, 1)[0]
      return ({
      fullPath,
      matched: [{}],
      meta: raw.startsWith('/agent-studio') ? { permissions: ['agent_asset:create'] } : {},
    })},
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

  it('preserves query parameters and anchors when navigating', async () => {
    harness.permission = true
    const wrapper = mount(AgentNavLink, {
      props: { href: '/reviews?tab=mine#latest', label: '我的审查' },
      global: { stubs: { 'el-icon': true } },
    })

    await wrapper.get('button').trigger('click')

    expect(harness.push).toHaveBeenCalledWith('/reviews?tab=mine#latest')
  })
})
