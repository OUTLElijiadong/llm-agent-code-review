import { defineComponent } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const teamApi = vi.hoisted(() => ({ events: vi.fn() }))

vi.mock('@/api/agentTeams', () => ({ listAgentTeamEvents: teamApi.events }))

import { useAgentTeamEvents } from './useAgentTeamEvents'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((ok, fail) => {
    resolve = ok
    reject = fail
  })
  return { promise, resolve, reject }
}

function mountHarness(onEvents = vi.fn(), onStatus = vi.fn()) {
  let controller!: ReturnType<typeof useAgentTeamEvents>
  const wrapper = mount(defineComponent({
    setup() {
      controller = useAgentTeamEvents({ interval: 1000, pageSize: 2, onEvents, onStatus })
      return () => null
    },
  }))
  return { wrapper, controller, onEvents, onStatus }
}

describe('useAgentTeamEvents', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    teamApi.events.mockReset()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('并行跟踪多个团队且旧请求不会污染其他团队', async () => {
    const first = deferred<any>()
    const second = deferred<any>()
    teamApi.events.mockImplementation((teamId: number) => teamId === 41 ? first.promise : second.promise)
    const harness = mountHarness()

    harness.controller.start(41)
    harness.controller.start(42)
    second.resolve({
      items: [{ event_id: 2, team_id: 42, event_type: 'team.created' }],
      has_more: false, next_after_id: 2, page_size: 2, team_status: 'running',
    })
    first.resolve({
      items: [{ event_id: 1, team_id: 41, event_type: 'team.created' }],
      has_more: false, next_after_id: 1, page_size: 2, team_status: 'running',
    })
    await flushPromises()

    expect(harness.onEvents.mock.calls.flatMap(([items]) => items).map((item) => item.team_id).sort()).toEqual([41, 42])
    expect(Object.keys(harness.controller.teamStatuses.value).sort()).toEqual(['41', '42'])
    harness.wrapper.unmount()
  })

  it('团队已终态时仍排空 has_more 后续页再停止', async () => {
    teamApi.events
      .mockResolvedValueOnce({
        items: [{ event_id: 1, team_id: 51, event_type: 'task.completed' }],
        has_more: true, next_after_id: 1, page_size: 1, team_status: 'completed',
      })
      .mockResolvedValueOnce({
        items: [{ event_id: 2, team_id: 51, event_type: 'team.status_changed', to_status: 'completed' }],
        has_more: false, next_after_id: 2, page_size: 1, team_status: 'completed',
      })
    const harness = mountHarness()

    harness.controller.start(51)
    await flushPromises()

    expect(teamApi.events).toHaveBeenCalledTimes(2)
    expect(harness.controller.events.value.map((item) => item.event_id)).toEqual([1, 2])
    await vi.advanceTimersByTimeAsync(5000)
    expect(teamApi.events).toHaveBeenCalledTimes(2)
    harness.wrapper.unmount()
  })

  it.each(['blocked', 'expired'])('%s 终态排空剩余事件后停止轮询', async (status) => {
    teamApi.events
      .mockResolvedValueOnce({
        items: [{ event_id: 1, team_id: 52, event_type: 'task.status_changed', to_status: status }],
        has_more: true, next_after_id: 1, page_size: 1, team_status: status,
      })
      .mockResolvedValueOnce({
        items: [{ event_id: 2, team_id: 52, event_type: 'team.status_changed', to_status: status }],
        has_more: false, next_after_id: 2, page_size: 1, team_status: status,
      })
    const harness = mountHarness()
    try {
      harness.controller.start(52)
      await flushPromises()
      expect(harness.controller.events.value.map(event => event.event_id)).toEqual([1, 2])
      expect(harness.controller.teamStatuses.value[52]).toBe(status)
      await vi.advanceTimersByTimeAsync(5000)
      expect(teamApi.events.mock.calls).toEqual([[52, 0, 2], [52, 1, 2]])
    } finally {
      harness.wrapper.unmount()
    }
  })

  it('403/404 权限类错误立即停止且不形成持续轮询', async () => {
    teamApi.events.mockRejectedValue({ code: 40431, message: '团队不存在' })
    const harness = mountHarness()

    harness.controller.start(61)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(5000)

    expect(teamApi.events).toHaveBeenCalledTimes(1)
    expect(harness.controller.error.value).toContain('团队不存在')
    harness.wrapper.unmount()
  })

  it.each([1, 2, 3])('第 %i 轮：多个活动团队保留独立增量游标，完成/失败/取消后停止', async () => {
    const statuses = new Map([[81, 'completed'], [82, 'failed'], [83, 'cancelled']])
    teamApi.events.mockImplementation((teamId: number, afterId: number) => Promise.resolve({
      items: [{ event_id: afterId ? teamId + 100 : teamId, team_id: teamId, event_type: afterId ? 'team.status_changed' : 'task.claimed' }],
      has_more: false, next_after_id: afterId ? teamId + 100 : teamId, page_size: 2,
      team_status: afterId ? statuses.get(teamId) : 'running',
    }))
    const harness = mountHarness()
    try {
      for (const id of statuses.keys()) harness.controller.start(id)
      await flushPromises()
      await vi.advanceTimersByTimeAsync(1000)
      expect(teamApi.events.mock.calls).toEqual([
        [81, 0, 2], [82, 0, 2], [83, 0, 2],
        [81, 81, 2], [82, 82, 2], [83, 83, 2],
      ])
      expect(harness.controller.events.value).toHaveLength(6)
      expect(harness.controller.teamStatuses.value).toEqual({ 81: 'completed', 82: 'failed', 83: 'cancelled' })
      await vi.advanceTimersByTimeAsync(5000)
      expect(teamApi.events).toHaveBeenCalledTimes(6)
    } finally {
      harness.wrapper.unmount()
    }
  })

  it('短暂 API 异常和未知状态不会误停有效团队，重试沿用已有游标', async () => {
    teamApi.events
      .mockResolvedValueOnce({ items: [{ event_id: 7, team_id: 91, event_type: 'task.claimed' }], has_more: false, next_after_id: 7, team_status: 'running' })
      .mockRejectedValueOnce({ code: 50000, message: '暂时无法读取' })
      .mockResolvedValueOnce({ items: [], has_more: false, next_after_id: 7, team_status: '' })
      .mockResolvedValueOnce({ items: [{ event_id: 8, team_id: 91, event_type: 'team.status_changed' }], has_more: false, next_after_id: 8, team_status: 'completed' })
    const harness = mountHarness()
    try {
      harness.controller.start(91)
      await flushPromises()
      await vi.advanceTimersByTimeAsync(1000)
      expect(harness.controller.teamStatuses.value[91]).toBe('running')
      await vi.advanceTimersByTimeAsync(2000)
      expect(harness.controller.teamStatuses.value[91]).toBe('running')
      await vi.advanceTimersByTimeAsync(1000)
      expect(teamApi.events.mock.calls).toEqual([[91, 0, 2], [91, 7, 2], [91, 7, 2], [91, 7, 2]])
      expect(harness.controller.events.value.map(event => event.event_id)).toEqual([7, 8])
      expect(harness.controller.teamStatuses.value[91]).toBe('completed')
      await vi.advanceTimersByTimeAsync(5000)
      expect(teamApi.events).toHaveBeenCalledTimes(4)
    } finally {
      harness.wrapper.unmount()
    }
  })
})
