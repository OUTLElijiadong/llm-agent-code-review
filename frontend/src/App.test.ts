import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const auth = vi.hoisted(() => ({
  token: 'token',
  profile: { id: 1, role: 'admin' } as { id: number; role: string } | null,
  agentPermission: true,
  state: {} as {
    token: string
    profile: { id: number; role: string } | null
    agentPermission: boolean
  },
  setupSecurityAlerts: vi.fn(),
  disposeSecurityAlerts: vi.fn(),
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
vi.mock('@/stores/user', async () => {
  const { reactive } = await import('vue')
  auth.state = reactive({
    token: auth.token,
    profile: auth.profile,
    agentPermission: auth.agentPermission,
  })
  return {
    useUserStore: () => ({
      get token() { return auth.state.token },
      get profile() { return auth.state.profile },
      isAdmin: () => ['admin', 'super_admin'].includes(auth.state.profile?.role ?? ''),
      hasPermission: (code: string) => code === 'agent:chat' && auth.state.agentPermission,
    }),
  }
})
vi.mock('@/composables/useSecurityAlerts', () => ({
  setupSecurityAlerts: auth.setupSecurityAlerts,
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
          props: ['visible', 'prefill', 'showLauncher', 'preferredSessionId', 'preferredSessionRequestId'],
          template: '<aside class="user-agent-stub" :data-visible="String(visible)" :data-prefill="prefill" :data-launcher="String(showLauncher)" :data-session-id="preferredSessionId" :data-session-request-id="preferredSessionRequestId" />',
        },
      },
    },
  })
}

describe('全局小菱宿主', () => {
  beforeEach(() => {
    auth.state.token = 'token'
    auth.state.profile = { id: 1, role: 'admin' }
    auth.state.agentPermission = true
    auth.setupSecurityAlerts.mockReset()
    auth.disposeSecurityAlerts.mockReset()
    auth.setupSecurityAlerts.mockReturnValue({ dispose: auth.disposeSecurityAlerts })
    router.beforeEach.mockClear()
    router.afterEach.mockClear()
    router.onError.mockClear()
  })

  it('管理员两个会话surface使用同一小菱身份并按入口切换', async () => {
    const forwardedToAdmin = vi.fn()
    window.addEventListener('prism:open-admin-copilot', forwardedToAdmin)
    const wrapper = mountApp()

    expect(auth.setupSecurityAlerts).toHaveBeenCalledTimes(1)

    expect(wrapper.find('.admin-copilot-stub').exists()).toBe(true)
    expect(wrapper.find('.user-agent-stub').exists()).toBe(true)
    expect(wrapper.get('.user-agent-stub').attributes('data-launcher')).toBe('false')

    window.dispatchEvent(new CustomEvent('prism:open-agent-chat', { detail: { prefill: '检查项目' } }))
    await nextTick()
    expect(forwardedToAdmin).not.toHaveBeenCalled()
    expect(wrapper.get('.user-agent-stub').attributes('data-visible')).toBe('true')
    expect(wrapper.get('.user-agent-stub').attributes('data-prefill')).toBe('检查项目')

    window.dispatchEvent(new Event('prism:admin-copilot-opened'))
    await nextTick()
    expect(wrapper.get('.user-agent-stub').attributes('data-visible')).toBe('false')

    wrapper.unmount()
    expect(auth.disposeSecurityAlerts).toHaveBeenCalledTimes(1)
    window.removeEventListener('prism:open-admin-copilot', forwardedToAdmin)
  })

  it('普通成员只挂载用户小菱并由全局事件打开', async () => {
    auth.state.profile = { id: 7, role: 'user' }
    const wrapper = mountApp()
    expect(auth.setupSecurityAlerts).not.toHaveBeenCalled()
    expect(wrapper.find('.admin-copilot-stub').exists()).toBe(false)
    expect(wrapper.get('.user-agent-stub').attributes('data-visible')).toBe('false')
    expect(wrapper.get('.user-agent-stub').attributes('data-launcher')).toBe('false')

    window.dispatchEvent(new CustomEvent('prism:open-agent-chat', { detail: { prefill: '分析代码' } }))
    await nextTick()
    expect(wrapper.get('.user-agent-stub').attributes('data-visible')).toBe('true')
    expect(wrapper.get('.user-agent-stub').attributes('data-prefill')).toBe('分析代码')
    wrapper.unmount()
  })

  it('待办唤起指定会话，并在当前抽屉关闭后清除首选会话', async () => {
    auth.state.profile = { id: 7, role: 'user' }
    const wrapper = mountApp()
    window.dispatchEvent(new CustomEvent('prism:open-agent-chat', { detail: { sessionId: 'owned-session-42' } }))
    await nextTick()

    const drawer = wrapper.get('.user-agent-stub')
    expect(drawer.attributes('data-session-id')).toBe('owned-session-42')
    expect(drawer.attributes('data-session-request-id')).toBe('1')

    window.dispatchEvent(new Event('prism:admin-copilot-opened'))
    await nextTick()
    expect(drawer.attributes('data-visible')).toBe('false')
    expect(drawer.attributes('data-session-id')).toBe('')
    wrapper.unmount()
  })

  it('缺少 agent:chat 权限时不挂载小菱也不响应唤起事件', async () => {
    auth.state.profile = { id: 8, role: 'user' }
    auth.state.agentPermission = false
    const wrapper = mountApp()

    expect(wrapper.find('.user-agent-stub').exists()).toBe(false)
    window.dispatchEvent(new CustomEvent('prism:open-agent-chat', { detail: { prefill: '越权唤起' } }))
    await nextTick()
    expect(wrapper.find('.user-agent-stub').exists()).toBe(false)
    wrapper.unmount()
  })

  it('管理员切换账号时关闭旧告警订阅并为新账号重新订阅', async () => {
    const wrapper = mountApp()
    auth.state.profile = { id: 2, role: 'super_admin' }
    auth.state.token = 'new-account-token'
    await nextTick()

    expect(auth.setupSecurityAlerts).toHaveBeenCalledTimes(2)
    expect(auth.disposeSecurityAlerts).toHaveBeenCalledTimes(1)

    wrapper.unmount()
    expect(auth.disposeSecurityAlerts).toHaveBeenCalledTimes(2)
  })
})
