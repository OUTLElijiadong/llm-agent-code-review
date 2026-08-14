import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ProactivePageGuide from './ProactivePageGuide.vue'

async function mountGuide(path: string, surface: 'user' | 'admin'): Promise<{ wrapper: ReturnType<typeof mount>; router: Router }> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/projects', component: { template: '<div>projects</div>' } },
      { path: '/login', component: { template: '<div>login</div>' } },
      { path: '/:pathMatch(.*)*', component: { template: '<div>fallback</div>' } },
    ],
  })
  router.push(path)
  await router.isReady()
  const wrapper = mount(ProactivePageGuide, {
    props: { surface },
    global: { plugins: [router] },
  })
  await flushPromises()
  return { wrapper, router }
}

describe('ProactivePageGuide', () => {
  beforeEach(() => { sessionStorage.clear() })
  afterEach(() => { sessionStorage.clear(); vi.restoreAllMocks() })

  it('进入有建议的页面时弹出引导', async () => {
    const { wrapper } = await mountGuide('/projects', 'user')
    expect(wrapper.find('.proactive-guide').exists()).toBe(true)
    expect(wrapper.text()).toContain('下一步建议')
  })

  it('点击引导派发用户端唤起事件并预填指令', async () => {
    const listener = vi.fn()
    window.addEventListener('prism:open-agent-chat', listener as EventListener)
    const { wrapper } = await mountGuide('/projects', 'user')
    await wrapper.find('.guide-act').trigger('click')
    expect(listener).toHaveBeenCalled()
    const event = listener.mock.calls[0][0] as CustomEvent<{ prefill: string }>
    expect(event.detail.prefill).toContain('项目管理')
    window.removeEventListener('prism:open-agent-chat', listener as EventListener)
  })

  it('切到已看过的页面时清空上一个页面的残留引导', async () => {
    const first = await mountGuide('/projects', 'user')
    expect(first.wrapper.find('.proactive-guide').exists()).toBe(true)
    first.wrapper.unmount()

    // 第二次进入 /projects 会被 sessionStorage 拦截;再切到 /login(无建议)后不应残留。
    const second = await mountGuide('/projects', 'user')
    expect(second.wrapper.find('.proactive-guide').exists()).toBe(false)
    await second.router.push('/login')
    await flushPromises()
    expect(second.wrapper.find('.proactive-guide').exists()).toBe(false)
    second.wrapper.unmount()
  })
  it('无建议的页面不弹出引导', async () => {
    const { wrapper } = await mountGuide('/login', 'user')
    expect(wrapper.find('.proactive-guide').exists()).toBe(false)
  })

  it('同一页面在当前会话只弹一次', async () => {
    const first = await mountGuide('/projects', 'user')
    expect(first.wrapper.find('.proactive-guide').exists()).toBe(true)
    first.wrapper.unmount()
    const second = await mountGuide('/projects', 'user')
    expect(second.wrapper.find('.proactive-guide').exists()).toBe(false)
  })
})
