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

function deferred<Value>() {
  let resolve!: (value: Value) => void
  const promise = new Promise<Value>((done) => { resolve = done })
  return { promise, resolve }
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

it('切换账号后旧分页请求不能锁住新账号或清除新请求状态', async () => {
  const oldPage = deferred<{ items: Array<typeof current>; next_offset: number | null }>()
  const newPage = deferred<{ items: Array<typeof current>; next_offset: number | null }>()
  discussionApi.list
    .mockResolvedValueOnce({ items: [current], next_offset: 1 })
    .mockReturnValueOnce(oldPage.promise)
    .mockResolvedValueOnce({ items: [{ ...current, session_id: 'new-account-session', file_name: 'new-account-session.py' }], next_offset: 2 })
    .mockReturnValueOnce(newPage.promise)
  const { wrapper } = await mountHost(5)
  try {
    await wrapper.get('[aria-label="打开圆桌讨论"]').trigger('click')
    await wrapper.get('.roundtable-load-more').trigger('click')
    expect(wrapper.get('.roundtable-load-more').attributes('disabled')).toBeDefined()

    await wrapper.setProps({ userId: 73 })
    await flushPromises()
    await wrapper.get('[aria-label="打开圆桌讨论"]').trigger('click')
    expect(wrapper.get('.roundtable-load-more').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('.roundtable-choices').text()).toContain('new-account-session.py')
    await wrapper.get('.roundtable-load-more').trigger('click')
    expect(wrapper.get('.roundtable-load-more').attributes('disabled')).toBeDefined()

    oldPage.resolve({ items: [{ ...current, session_id: 'old-account-only', file_name: 'old-account-only.py' }], next_offset: null })
    await flushPromises()
    expect(wrapper.get('.roundtable-load-more').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).not.toContain('old-account-only.py')

    newPage.resolve({ items: [{ ...current, session_id: 'new-account-older', file_name: 'new-account-older.py' }], next_offset: null })
    await flushPromises()
    expect(wrapper.find('.roundtable-load-more').exists()).toBe(false)
    expect(wrapper.get('.roundtable-choices').text()).toContain('new-account-session.py')
    expect(wrapper.get('.roundtable-choices').text()).toContain('new-account-older.py')
    expect(wrapper.get('.roundtable-choices').text()).not.toContain('old-account-only.py')
  } finally {
    wrapper.unmount()
  }
})

it('同账号列表刷新使分页响应过期后，会解除加载状态并允许重试', async () => {
  const oldPage = deferred<{ items: Array<typeof current>; next_offset: number | null }>()
  const pageOffsets: number[] = []
  discussionApi.list.mockImplementation((_limit?: number, offset?: number) => {
    if (offset === undefined) return Promise.resolve({ items: [current], next_offset: 1 })
    pageOffsets.push(offset)
    return pageOffsets.length === 1
      ? oldPage.promise
      : Promise.resolve({ items: [{ ...current, session_id: 'refreshed-older', file_name: 'refreshed-older.py' }], next_offset: null })
  })
  const { wrapper } = await mountHost(5)
  try {
    await wrapper.get('[aria-label="打开圆桌讨论"]').trigger('click')
    await wrapper.get('.roundtable-load-more').trigger('click')
    expect(wrapper.get('.roundtable-load-more').attributes('disabled')).toBeDefined()

    window.dispatchEvent(new CustomEvent('prism:roundtable-list-changed', { detail: { ownerUserId: 5 } }))
    await flushPromises()
    oldPage.resolve({ items: [{ ...current, session_id: 'stale-older', file_name: 'stale-older.py' }], next_offset: null })
    await flushPromises()

    expect(wrapper.get('.roundtable-load-more').attributes('disabled')).toBeUndefined()
    await wrapper.get('.roundtable-load-more').trigger('click')
    await flushPromises()
    expect(pageOffsets).toEqual([1, 1])
    expect(wrapper.text()).toContain('refreshed-older.py')
    expect(wrapper.text()).not.toContain('stale-older.py')
  } finally {
    wrapper.unmount()
  }
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

it('入口角标统计可继续的圆桌，已结束会话仍可从入口打开', async () => {
  discussionApi.list.mockResolvedValue({
    items: [current, { ...current, session_id: 'disc-paused', status: 'paused' }, { ...current, session_id: 'disc-ended', status: 'concluded' }],
    next_offset: null,
  })
  const { wrapper } = await mountHost()
  expect(wrapper.get('.roundtable-count').text()).toBe('2')
  expect(wrapper.get('[aria-label="打开圆桌讨论"]').attributes('title')).toContain('已结束 1 个')

  discussionApi.list.mockResolvedValue({ items: [{ ...current, session_id: 'disc-ended', status: 'concluded' }], next_offset: null })
  await wrapper.setProps({ userId: 6 })
  await flushPromises()
  expect(wrapper.find('.roundtable-count').exists()).toBe(false)
  expect(wrapper.find('[aria-label="打开圆桌讨论"]').exists()).toBe(true)
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

it('全局顶栏存在时将圆桌入口挂在顶栏中，避免固定浮层盖住页面内容', async () => {
  const target = document.createElement('div')
  target.id = 'roundtable-header-slot'
  document.body.appendChild(target)
  const { wrapper } = await mountHost()
  await flushPromises()

  expect(target.querySelector('.roundtable-dock')).not.toBeNull()
  expect(wrapper.find('.roundtable-dock').exists()).toBe(false)

  wrapper.unmount()
  target.remove()
})

it('结束后的五分钟追问窗口显示为可追问并计入入口角标，超时后显示已结束', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-09-30T10:00:00Z'))
  const now = Math.floor(Date.now() / 1000)
  discussionApi.list.mockResolvedValue({
    items: [
      { ...current, session_id: 'disc-followup', status: 'concluded', followup_until: now + 300 },
      { ...current, session_id: 'disc-expired', status: 'concluded', followup_until: now - 1 },
    ],
    next_offset: null,
  })
  let wrapper: Awaited<ReturnType<typeof mountHost>>['wrapper'] | null = null
  try {
    ({ wrapper } = await mountHost())
    expect(wrapper.get('.roundtable-count').text()).toBe('1')
    expect(wrapper.get('[aria-label="打开圆桌讨论"]').attributes('title')).toContain('已结束 1 个')

    await wrapper.get('[aria-label="打开圆桌讨论"]').trigger('click')
    expect(wrapper.findAll('.roundtable-choice').map((choice) => choice.text()).join(' ')).toContain('追问中')
    expect(wrapper.findAll('.roundtable-choice').map((choice) => choice.text()).join(' ')).toContain('已结束')

    await vi.advanceTimersByTimeAsync(299_000)
    expect(wrapper.get('.roundtable-count').text()).toBe('1')
    await vi.advanceTimersByTimeAsync(1000)
    expect(wrapper.find('.roundtable-count').exists()).toBe(false)
    expect(wrapper.get('[aria-label="打开圆桌讨论"]').attributes('title')).toContain('已结束 2 个')
    expect(wrapper.findAll('.roundtable-choice').map((choice) => choice.text()).join(' ')).not.toContain('追问中')
  } finally {
    wrapper?.unmount()
    vi.useRealTimers()
  }
})
