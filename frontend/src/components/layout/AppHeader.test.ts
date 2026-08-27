import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const harness = vi.hoisted(() => ({
  permission: true,
  push: vi.fn(),
  replace: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/dashboard', matched: [], meta: { title: '工作台' } }),
  useRouter: () => ({
    push: harness.push,
    replace: harness.replace,
    resolve: ({ path }: { path: string }) => ({
      matched: [{}],
      meta: path === '/agent-studio' ? { permissions: ['agent_asset:create'] } : {},
    }),
  }),
}))

vi.mock('@/stores/user', () => ({
  useUserStore: () => ({
    profile: { id: 7, role: 'user' },
    token: 'token',
    displayName: '普通成员',
    isAdmin: () => false,
    isSuperAdmin: () => false,
    hasRole: (role: string) => role === 'user',
    hasPermission: () => harness.permission,
    logout: vi.fn(),
  }),
}))

import AppHeader from './AppHeader.vue'

function mountHeader() {
  return mount(AppHeader, {
    global: {
      provide: { openAgentChat: vi.fn() },
      stubs: {
        'el-icon': true,
        'el-dialog': { template: '<section><slot /></section>' },
        'el-input': true,
        'el-dropdown': { template: '<div><slot /><slot name="dropdown" /></div>' },
        'el-dropdown-menu': { template: '<div><slot /></div>' },
        'el-dropdown-item': { template: '<button><slot /></button>' },
      },
    },
  })
}

describe('AppHeader navigation visibility', () => {
  beforeEach(() => {
    harness.permission = true
    harness.push.mockReset()
    harness.replace.mockReset()
  })

  it('uses the same permission gate for search results', () => {
    harness.permission = false
    expect(mountHeader().text()).not.toContain('Agent 工坊')

    harness.permission = true
    expect(mountHeader().text()).toContain('Agent 工坊')
  })

  it('renders accessible top navigation with active underline state', () => {
    const wrapper = mountHeader()
    const dashboard = wrapper.get('.header-nav-item[data-route="/dashboard"]')

    expect(dashboard.classes()).toContain('is-active')
    expect(dashboard.attributes('aria-current')).toBe('page')
    expect(wrapper.get('.agent-trigger').attributes('aria-label')).toBe('打开小菱助手')
  })
})
