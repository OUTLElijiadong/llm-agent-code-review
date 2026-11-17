import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAgentActivityStore } from './agentActivity'

describe('agent activity surface isolation', () => {
  beforeEach(() => { setActivePinia(createPinia()); vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('同 surface 的不同流即使调用 ID 相同也独立结束与清理', () => {
    const store = useAgentActivityStore()
    store.begin('会话一', 'same', undefined, 'user', 'stream-1', 'session-1')
    store.begin('会话二', 'same', undefined, 'user', 'stream-2', 'session-2')
    expect(store.count).toBe(2)
    store.end('same', 'user', 'stream-1')
    expect(store.count).toBe(1)
    store.clear('user', 'stream-1')
    expect(store.current?.label).toBe('会话二')
    store.clear('user', 'stream-2')
    expect(store.count).toBe(0)
  })

  it('两个 surface 的相同调用 ID 不冲突，清理一个不影响另一个', () => {
    const store = useAgentActivityStore()
    store.begin('代码运行', 'same-call', undefined, 'user')
    store.begin('管理运行', 'same-call', undefined, 'admin')
    expect(store.count).toBe(2)
    store.clear('user')
    expect(store.count).toBe(1)
    expect(store.current?.label).toBe('管理运行')
    store.clear('admin')
    expect(store.count).toBe(0)
  })

  it('未知的结束 ID 不得误删任何其他任务', () => {
    const store = useAgentActivityStore()
    store.begin('代码运行', 'user-call', undefined, 'user')
    store.begin('管理运行', 'admin-call', undefined, 'admin')
    store.end('missing-call', 'user')
    expect(store.count).toBe(2)
  })

  it('清理一个 surface 不取消另一 surface 的延迟完成', () => {
    const store = useAgentActivityStore()
    store.begin('代码运行', 'user-call', undefined, 'user')
    store.begin('管理运行', 'admin-call', undefined, 'admin')
    store.complete('admin-call', 100, 'admin')
    store.clear('user')
    expect(store.current?.label).toBe('管理运行')
    vi.advanceTimersByTime(100)
    expect(store.count).toBe(0)
  })

  it('同一调用 ID 重用时，旧的完成计时器不清除新活动', () => {
    const store = useAgentActivityStore()
    store.begin('旧活动', 'same-call', undefined, 'user')
    store.complete('same-call', 100, 'user')
    store.begin('新活动', 'same-call', undefined, 'user')
    vi.advanceTimersByTime(100)
    expect(store.current?.label).toBe('新活动')
  })

  it('重启已有活动后展示最新事件而非另一个较早活动', () => {
    const store = useAgentActivityStore()
    store.begin('代码旧进度', 'same-call', undefined, 'user')
    store.begin('管理进度', 'admin-call', undefined, 'admin')
    store.begin('代码新进度', 'same-call', undefined, 'user')
    expect(store.current?.label).toBe('代码新进度')
  })

  it('结束时与开始时一致地规范化调用 ID，空 ID 不误删', () => {
    const store = useAgentActivityStore()
    store.begin('代码运行', ' task ', undefined, 'user')
    store.end('  ', 'user')
    expect(store.count).toBe(1)
    store.end(' task ', 'user')
    expect(store.count).toBe(0)
  })
})
