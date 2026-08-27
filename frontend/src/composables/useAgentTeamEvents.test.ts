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
})
