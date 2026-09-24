import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, expect, it, vi } from 'vitest'

const discussionApi = vi.hoisted(() => ({ list: vi.fn(), detail: vi.fn() }))
vi.mock('@/api/discussion', () => ({
  listDiscussionSessions: discussionApi.list,
  getDiscussionSession: discussionApi.detail,
}))

import GlobalDiscussionHost from './GlobalDiscussionHost.vue'

const current = {
  session_id: 'disc-own', status: 'active', file_name: 'dashboard_routes.py',
  agents: [{ code: 'security', name: '安全审查代理' }], ws_url: '/api/ws/discuss/disc-own',
  report_task_id: 0, max_rounds: 2, progress: { phase: 'speaking', completed_units: 1, total_units: 10, current_round: 1, seq: 2 },
}

async function mountHost(userId = 5) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/dashboard', component: { template: '<div />' } }, { path: '/agents', component: { template: '<div />' } }],
  })
  await router.push('/dashboard')
  await router.isReady()
  const wrapper = mount(GlobalDiscussionHost, {
    props: { userId },
    global: {
      plugins: [router],
      stubs: { AgentDiscussionPanel: {
        name: 'AgentDiscussionPanel',
        props: ['sessionId', 'initialTurns', 'initialHasEarlier', 'initialNextBeforeSeq'],
        template: '<div data-testid="discussion-panel">{{ sessionId }}</div>',
      } },
    },
  })
  await flushPromises()
  return { wrapper, router }
}

beforeEach(() => {
  discussionApi.list.mockReset().mockResolvedValue({ items: [current], next_offset: null })
  discussionApi.detail.mockReset().mockResolvedValue(current)
})

it('跨页有圆桌入口，关闭仅隐藏，仍可从全局入口重开', async () => {
  const { wrapper, router } = await mountHost()
  expect(wrapper.get('[aria-label="打开圆桌讨论"]')).toBeTruthy()

  await wrapper.get('[aria-label="打开圆桌讨论"]').trigger('click')
  await flushPromises()
  expect(wrapper.get('[data-testid="discussion-panel"]').text()).toBe('disc-own')

  await router.push('/agents')
  await flushPromises()
  expect(wrapper.find('[data-testid="discussion-panel"]').exists()).toBe(true)

  wrapper.findComponent({ name: 'AgentDiscussionPanel' }).vm.$emit('close')
  await flushPromises()
  expect(wrapper.find('[data-testid="discussion-panel"]').exists()).toBe(false)
  await wrapper.get('[aria-label="打开圆桌讨论"]').trigger('click')
  await flushPromises()
  expect(wrapper.find('[data-testid="discussion-panel"]').exists()).toBe(true)
  wrapper.unmount()
})

it('仅凭会话 ID 的续会链接也从服务端加载元数据，并在账号切换时丢弃旧账号结果', async () => {
  const { wrapper, router } = await mountHost()
  await router.push('/agents?discuss_session=disc-own')
  await flushPromises()
  expect(discussionApi.detail).toHaveBeenCalledWith('disc-own')
  expect(wrapper.get('[data-testid="discussion-panel"]').text()).toBe('disc-own')

  discussionApi.list.mockResolvedValue({ items: [], next_offset: null })
  await wrapper.setProps({ userId: 73 })
  await flushPromises()
  expect(wrapper.find('[data-testid="discussion-panel"]').exists()).toBe(false)
  expect(wrapper.find('[aria-label="打开圆桌讨论"]').exists()).toBe(false)
  wrapper.unmount()
})

it('旧账号详情的迟到响应不能在新账号显示', async () => {
  let resolveOld!: (value: typeof current) => void
  discussionApi.detail.mockImplementation(() => new Promise((resolve) => { resolveOld = resolve }))
  const { wrapper, router } = await mountHost()
  await router.push('/agents?discuss_session=disc-own')
  await flushPromises()
  discussionApi.list.mockResolvedValue({ items: [], next_offset: null })
  await wrapper.setProps({ userId: 73 })
  resolveOld(current)
  await flushPromises()
  expect(wrapper.find('[data-testid="discussion-panel"]').exists()).toBe(false)
  wrapper.unmount()
})

it('圆桌超过首批列表时可逐页加载，单条首批也能打开选择器', async () => {
  discussionApi.list
    .mockResolvedValueOnce({ items: [current], next_offset: 1 })
    .mockResolvedValueOnce({ items: [{ ...current, session_id: 'disc-older', file_name: 'older.py' }], next_offset: null })
  const { wrapper } = await mountHost()
  await wrapper.get('[aria-label="打开圆桌讨论"]').trigger('click')
  expect(wrapper.find('[data-testid="discussion-panel"]').exists()).toBe(false)
  expect(wrapper.get('.roundtable-choices').text()).toContain('dashboard_routes.py')
  await wrapper.get('.roundtable-load-more').trigger('click')
  await flushPromises()
  expect(discussionApi.list).toHaveBeenCalledWith(30, 1)
  expect(wrapper.get('.roundtable-choices').text()).toContain('older.py')
  wrapper.unmount()
})

it('会话列表准确区分有有效部分报告的圆桌', async () => {
  discussionApi.list.mockResolvedValue({
    items: [current, {
      ...current, session_id: 'disc-partial', status: 'concluded',
      progress: { ...current.progress, phase: 'partial' },
    }], next_offset: null,
  })
  const { wrapper } = await mountHost()
  await wrapper.get('[aria-label="打开圆桌讨论"]').trigger('click')
  expect(wrapper.get('.roundtable-choices').text()).toContain('部分完成')
  wrapper.unmount()
})

it('服务端详情的历史页与更早游标交给圆桌面板', async () => {
  discussionApi.detail.mockResolvedValue({
    ...current, turns: [{ seq: 51, turn_id: 51, role: 'agent', content: '历史发言' }],
    has_earlier: true, next_before_seq: 51,
  })
  const { wrapper } = await mountHost()
  await wrapper.get('[aria-label="打开圆桌讨论"]').trigger('click')
  await flushPromises()
  const panel = wrapper.findComponent({ name: 'AgentDiscussionPanel' })
  expect(panel.props('initialTurns')).toHaveLength(1)
  expect(panel.props('initialHasEarlier')).toBe(true)
  expect(panel.props('initialNextBeforeSeq')).toBe(51)
  wrapper.unmount()
})

it('同标签页的小菱后台创建圆桌后，当前账号入口无需点链接即可出现', async () => {
  discussionApi.list
    .mockResolvedValueOnce({ items: [], next_offset: null })
    .mockResolvedValueOnce({ items: [current], next_offset: null })
  const { wrapper } = await mountHost(5)
  expect(wrapper.find('[aria-label="打开圆桌讨论"]').exists()).toBe(false)
  window.dispatchEvent(new CustomEvent('prism:roundtable-list-changed', { detail: { ownerUserId: 73 } }))
  await flushPromises()
  expect(discussionApi.list).toHaveBeenCalledTimes(1)
  window.dispatchEvent(new CustomEvent('prism:roundtable-list-changed', { detail: { ownerUserId: 5 } }))
  await flushPromises()
  expect(discussionApi.list).toHaveBeenCalledTimes(2)
  expect(wrapper.get('[aria-label="打开圆桌讨论"]')).toBeTruthy()
  wrapper.unmount()
})
