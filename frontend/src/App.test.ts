import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const auth = vi.hoisted(() => ({
  token: 'token',
  profile: { id: 1, role: 'admin' } as { id: number; role: string } | null,
}))

const router = vi.hoisted(() => ({
  beforeEach: vi.fn(() => vi.fn()),
  afterEach: vi.fn(() => vi.fn()),
  onError: vi.fn(() => vi.fn()),
}))

vi.mock('vue-router', async (importOriginal) => ({
  ...await importOriginal<typeof import('vue-router')>(),
  useRouter: () => router,
}))
vi.mock('@/stores/user', () => ({
  useUserStore: () => ({
    get token() { return auth.token },
    get profile() { return auth.profile },
    isAdmin: () => ['admin', 'super_admin'].includes(auth.profile?.role ?? ''),
  }),
}))

import App from './App.vue'

function mountApp() {
  return mount(App, {
    global: {
      stubs: {
        'router-view': { template: '<main class="router-view-stub" />' },
        Transition: false,
        PrismLoading: true,
        AgentActivityBorder: true,
        VirtualCursor: true,
        AdminCopilot: { template: '<aside class="admin-copilot-stub" />' },
        AgentChatDrawer: {
          props: ['visible', 'prefill'],
          template: '<aside class="user-agent-stub" :data-visible="String(visible)" :data-prefill="prefill" />',
        },
      },
    },
  })
}

describe('全局小菱宿主', () => {
  beforeEach(() => {
    auth.token = 'token'
    auth.profile = { id: 1, role: 'admin' }
    router.beforeEach.mockClear()
    router.afterEach.mockClear()
    router.onError.mockClear()
  })

  it('管理员只挂载管理小菱,跨布局导航不会替换为用户端小菱', async () => {
    const forwarded = vi.fn()
    window.addEventListener('prism:open-admin-copilot', forwarded)
    const wrapper = mountApp()

    expect(wrapper.find('.admin-copilot-stub').exists()).toBe(true)
    expect(wrapper.find('.user-agent-stub').exists()).toBe(false)

    window.dispatchEvent(new CustomEvent('prism:open-agent-chat', { detail: { prefill: '检查项目' } }))
    await nextTick()
    expect(forwarded).toHaveBeenCalledOnce()
    expect((forwarded.mock.calls[0][0] as CustomEvent).detail).toEqual({ prefill: '检查项目' })

    wrapper.unmount()
    window.removeEventListener('prism:open-admin-copilot', forwarded)
  })

  it('普通成员只挂载用户小菱并由全局事件打开', async () => {
    auth.profile = { id: 7, role: 'user' }
    const wrapper = mountApp()
    expect(wrapper.find('.admin-copilot-stub').exists()).toBe(false)
    expect(wrapper.get('.user-agent-stub').attributes('data-visible')).toBe('false')

    window.dispatchEvent(new CustomEvent('prism:open-agent-chat', { detail: { prefill: '分析代码' } }))
    await nextTick()
    expect(wrapper.get('.user-agent-stub').attributes('data-visible')).toBe('true')
    expect(wrapper.get('.user-agent-stub').attributes('data-prefill')).toBe('分析代码')
    wrapper.unmount()
  })
})
