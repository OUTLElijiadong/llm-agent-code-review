import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'

import type { DiscussionTurn } from '@/utils/discussionStream'

const streamMock = vi.hoisted(() => ({ subscribe: vi.fn() }))
const discussionApi = vi.hoisted(() => ({ detail: vi.fn() }))
vi.mock('@/utils/discussionStream', () => ({ subscribeDiscussion: streamMock.subscribe }))
vi.mock('@/api/discussion', () => ({ getDiscussionSession: discussionApi.detail }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

import AgentDiscussionPanel from './AgentDiscussionPanel.vue'

afterEach(() => {
  vi.useRealTimers()
})

it('100 条发言连接后每 20 秒只对账一次，离页停止请求', async () => {
  vi.useFakeTimers()
  const turns: DiscussionTurn[] = Array.from({ length: 100 }, (_, index) => ({
    turn_id: index + 1,
    seq: index + 1,
    agent_code: 'security',
    agent_name: '安全审查代理',
    role: 'agent',
    content: `结论 ${index + 1}`,
    action: 'speak',
    timestamp: '2026-09-27T00:00:00Z',
  }))
  discussionApi.detail.mockReset().mockResolvedValue({
    turns,
    status: 'running',
    has_earlier: false,
    next_before_seq: null,
  })
  const close = vi.fn()
  let connected: ((status: 'connecting' | 'connected' | 'disconnected' | 'error') => void) | undefined
  streamMock.subscribe.mockReset().mockImplementation((_id, _onMessage, options) => {
    connected = options.onStatus
    return { send: vi.fn(), close }
  })
  const wrapper = mount(AgentDiscussionPanel, {
    props: { sessionId: 'c31-local', fileName: 'sample.py', agents: [], initialTurns: turns },
    global: { stubs: {
      Teleport: true,
      'el-button': { template: '<button><slot /></button>' },
      'el-tag': { template: '<span><slot /></span>' },
      'el-tooltip': { template: '<div><slot /></div>' },
      PrismLoading: true,
      EmptyState: true,
    } },
  })
  expect(connected).toBeTypeOf('function')
  connected?.('connected')
  await flushPromises()
  expect(discussionApi.detail).toHaveBeenCalledTimes(1)

  await vi.advanceTimersByTimeAsync(19_999)
  expect(discussionApi.detail).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(1)
  expect(discussionApi.detail).toHaveBeenCalledTimes(2)
  await vi.advanceTimersByTimeAsync(20_000)
  expect(discussionApi.detail).toHaveBeenCalledTimes(3)

  wrapper.unmount()
  await vi.advanceTimersByTimeAsync(40_000)
  expect(discussionApi.detail).toHaveBeenCalledTimes(3)
  expect(close).toHaveBeenCalled()
})
