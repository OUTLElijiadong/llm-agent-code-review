import { effectScope, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ pending: vi.fn() }))
vi.mock('@/api/remotePentest', () => ({ getPendingRemoteApprovalSnapshot: api.pending }))

import {
  PENDING_REMOTE_APPROVALS_POLL_MS,
  REMOTE_PENTEST_CHANGED_EVENT,
  usePendingRemoteApprovals,
} from './usePendingRemoteApprovals'

function mountComposable(enabled = true, userId: number | undefined = 7) {
  const scope = effectScope()
  const enabledRef = ref(enabled)
  const result = scope.run(() => usePendingRemoteApprovals({
    enabled: () => enabledRef.value,
    currentUserId: () => userId,
  }))!
  return { scope, enabledRef, ...result }
}

/** 假定时器下不能用 flushPromises，这里直接让出若干个微任务。 */
async function settle() {
  for (let index = 0; index < 12; index += 1) await Promise.resolve()
}

describe('usePendingRemoteApprovals', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    api.pending.mockReset()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('只统计别人提交的待审批任务，自己提交的不计入', async () => {
    api.pending.mockResolvedValue({
      total: 4,
      items: [{ owner_id: 7 }, { owner_id: 8 }, { owner_id: 9 }, { owner_id: 7 }],
    })
    const { scope, count } = mountComposable()
    await settle()

    expect(api.pending).toHaveBeenCalledTimes(1)
    expect(count.value).toBe(2)
    scope.stop()
  })

  it('没有审批权限时不请求接口，计数为 0', async () => {
    const { scope, count } = mountComposable(false)
    await settle()

    expect(api.pending).not.toHaveBeenCalled()
    expect(count.value).toBe(0)
    scope.stop()
  })

  it('接口失败时静默保留旧计数，不抛出未处理的异常', async () => {
    api.pending.mockResolvedValueOnce({ total: 1, items: [{ owner_id: 8 }] })
    const { scope, count, refresh } = mountComposable()
    await settle()
    expect(count.value).toBe(1)

    api.pending.mockRejectedValueOnce(new Error('network down'))
    await expect(refresh()).resolves.toBeUndefined()
    expect(count.value).toBe(1)
    scope.stop()
  })

  it('每 60 秒轮询一次，不会更频繁', async () => {
    api.pending.mockResolvedValue({ total: 0, items: [] })
    const { scope } = mountComposable()
    await settle()
    expect(PENDING_REMOTE_APPROVALS_POLL_MS).toBe(60_000)

    await vi.advanceTimersByTimeAsync(PENDING_REMOTE_APPROVALS_POLL_MS - 1)
    expect(api.pending).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(api.pending).toHaveBeenCalledTimes(2)
    scope.stop()
  })

  it('页面在后台时跳过本轮轮询', async () => {
    api.pending.mockResolvedValue({ total: 0, items: [] })
    const { scope } = mountComposable()
    await settle()
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)

    await vi.advanceTimersByTimeAsync(PENDING_REMOTE_APPROVALS_POLL_MS)
    expect(api.pending).toHaveBeenCalledTimes(1)

    hidden.mockReturnValue(false)
    await vi.advanceTimersByTimeAsync(PENDING_REMOTE_APPROVALS_POLL_MS)
    expect(api.pending).toHaveBeenCalledTimes(2)
    scope.stop()
  })

  it('批准或申请动作发出变更事件后立即刷新', async () => {
    api.pending.mockResolvedValueOnce({ total: 1, items: [{ owner_id: 8 }] })
    const { scope, count } = mountComposable()
    await settle()
    expect(count.value).toBe(1)

    api.pending.mockResolvedValueOnce({ total: 0, items: [] })
    window.dispatchEvent(new Event(REMOTE_PENTEST_CHANGED_EVENT))
    await settle()

    expect(api.pending).toHaveBeenCalledTimes(2)
    expect(count.value).toBe(0)
    scope.stop()
  })

  it('权限从无到有时开始统计，卸载后清理定时器和事件监听', async () => {
    api.pending.mockResolvedValue({ total: 1, items: [{ owner_id: 8 }] })
    const { scope, enabledRef, count } = mountComposable(false)
    await settle()
    expect(count.value).toBe(0)

    enabledRef.value = true
    await settle()
    expect(count.value).toBe(1)

    scope.stop()
    api.pending.mockClear()
    await vi.advanceTimersByTimeAsync(PENDING_REMOTE_APPROVALS_POLL_MS * 2)
    window.dispatchEvent(new Event(REMOTE_PENTEST_CHANGED_EVENT))
    await settle()
    expect(api.pending).not.toHaveBeenCalled()
  })

  it('失去审批权限后计数清零', async () => {
    api.pending.mockResolvedValue({ total: 1, items: [{ owner_id: 8 }] })
    const { scope, enabledRef, count } = mountComposable()
    await settle()
    expect(count.value).toBe(1)

    enabledRef.value = false
    await settle()
    expect(count.value).toBe(0)
    scope.stop()
  })
})
