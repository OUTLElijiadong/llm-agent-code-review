import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  heartbeat: vi.fn(),
  inbox: vi.fn(),
}))

vi.mock('@/api/agentMesh', () => ({
  heartbeatAgentMesh: api.heartbeat,
  pullAgentMeshInbox: api.inbox,
  AGENT_MESH_SESSION_GONE_CODE: 40321,
}))

import { createAgentMeshBridge } from './agentMeshBridge'

describe('Agent Mesh session bridge', () => {
  beforeEach(() => {
    api.heartbeat.mockReset().mockResolvedValue({})
    api.inbox.mockReset().mockResolvedValue([])
  })

  it('刷新心跳并且仅在会话空闲时认领一条消息', async () => {
    let busy = true
    const receive = vi.fn().mockResolvedValue(true)
    api.inbox.mockResolvedValue([
      { message_id: 'msg_a', status: 'delivered', subject: 'A' },
      { message_id: 'msg_b', status: 'delivered', subject: 'B' },
    ])
    const bridge = createAgentMeshBridge({
      surface: 'user',
      getSessionId: () => 'session-test-01',
      getTitle: () => '页面测试',
      isBusy: () => busy,
      onMessage: receive,
    })

    await bridge.syncNow()
    expect(api.heartbeat).toHaveBeenCalledOnce()
    expect(api.inbox).not.toHaveBeenCalled()

    busy = false
    await bridge.syncNow()
    expect(receive).toHaveBeenCalledOnce()
    expect(receive).toHaveBeenCalledWith(
      expect.objectContaining({ message_id: 'msg_a' }),
      'session-test-01',
    )

    await bridge.syncNow()
    expect(receive).toHaveBeenCalledTimes(2)
    expect(receive).toHaveBeenLastCalledWith(
      expect.objectContaining({ message_id: 'msg_b' }),
      'session-test-01',
    )
  })

  it('同一消息处理失败后可在下次轮询重试', async () => {
    const receive = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true)
    api.inbox.mockResolvedValue([{ message_id: 'msg_retry', status: 'delivered', subject: '重试' }])
    const bridge = createAgentMeshBridge({
      surface: 'admin',
      getSessionId: () => 'session-admin-01',
      getTitle: () => '运维管理',
      isBusy: () => false,
      onMessage: receive,
    })

    await bridge.syncNow()
    await bridge.syncNow()
    expect(receive).toHaveBeenCalledTimes(2)
  })

  it('被策略延后的消息只写完成回执,不会进入 Responses', async () => {
    const receive = vi.fn().mockResolvedValue(true)
    const deferred = vi.fn().mockResolvedValue(true)
    api.inbox.mockResolvedValue([{
      message_id: 'msg_jarvis',
      status: 'delivered',
      subject: 'JARVIS 运维简报',
      payload: { patrol_kind: 'jarvis' },
    }])
    const bridge = createAgentMeshBridge({
      surface: 'admin',
      getSessionId: () => 'session-admin-cost-01',
      getTitle: () => '管理对话',
      isBusy: () => false,
      onMessage: receive,
      shouldAutoProcess: (message) => message.payload.patrol_kind !== 'jarvis',
      onDeferredMessage: deferred,
    })

    await bridge.syncNow()
    await bridge.syncNow()

    expect(deferred).toHaveBeenCalledOnce()
    expect(deferred).toHaveBeenCalledWith(expect.objectContaining({ message_id: 'msg_jarvis' }), 'session-admin-cost-01')
    expect(receive).not.toHaveBeenCalled()
  })

  it('优先认领当前会话消息,再处理后台会话', async () => {
    const receive = vi.fn().mockResolvedValue(true)
    const pullOrder: string[] = []
    api.inbox.mockImplementation((_surface: string, sessionId: string) => {
      pullOrder.push(sessionId)
      return Promise.resolve([{ message_id: `msg_${sessionId}`, status: 'delivered', subject: '主动消息' }])
    })
    const bridge = createAgentMeshBridge({
      surface: 'admin',
      getSessionId: () => 'session-current-01',
      getTitle: () => '当前会话',
      getSessions: () => [
        { id: 'session-background-01', title: '后台会话' },
        { id: 'session-current-01', title: '当前会话' },
      ],
      isBusy: () => false,
      onMessage: receive,
    })

    await bridge.syncNow()

    expect(pullOrder[0]).toBe('session-current-01')
    expect(receive).toHaveBeenCalledWith(
      expect.objectContaining({ message_id: 'msg_session-current-01' }),
      'session-current-01',
    )
  })
  it('同步同一入口的全部本地会话并可认领后台会话消息', async () => {
    const receive = vi.fn().mockResolvedValue(true)
    api.inbox.mockImplementation((_surface: string, sessionId: string) => Promise.resolve(
      sessionId === 'session-background'
        ? [{ message_id: 'msg_background', status: 'delivered', subject: '后台任务' }]
        : [],
    ))
    const bridge = createAgentMeshBridge({
      surface: 'user',
      getSessionId: () => 'session-current-01',
      getTitle: () => '当前会话',
      getSessions: () => [
        { id: 'session-current-01', title: '当前会话' },
        { id: 'session-background', title: '后台会话' },
      ],
      isBusy: () => false,
      onMessage: receive,
    })

    await bridge.syncNow()

    expect(api.heartbeat).toHaveBeenCalledTimes(2)
    expect(receive).toHaveBeenCalledWith(
      expect.objectContaining({ message_id: 'msg_background' }),
      'session-background',
    )
  })

  it('轮询命中 40321(会话已归档)时静默跳过并通知 onSessionGone,不再对该会话重试', async () => {
    const receive = vi.fn().mockResolvedValue(true)
    const onSessionGone = vi.fn()
    // 归档会话的 inbox 拉取被后端以 40321 拒绝(Resp 形态 reject)
    api.inbox.mockImplementation((_surface: string, sessionId: string) => (
      sessionId === 'session-archived'
        ? Promise.reject({ code: 40321, message: '目标会话尚未注册或不属于当前账户' })
        : Promise.resolve([])
    ))
    const bridge = createAgentMeshBridge({
      surface: 'user',
      getSessionId: () => 'session-current-01',
      getTitle: () => '当前会话',
      getSessions: () => [
        { id: 'session-current-01', title: '当前会话' },
        { id: 'session-archived', title: '已归档会话' },
      ],
      isBusy: () => false,
      onMessage: receive,
      onSessionGone,
    })

    await bridge.syncNow()
    expect(onSessionGone).toHaveBeenCalledWith('session-archived')

    // 下一轮:归档会话不再 heartbeat 也不再 pull,不会反复触发 403
    api.heartbeat.mockClear()
    api.inbox.mockClear()
    await bridge.syncNow()
    const heartbeatIds = api.heartbeat.mock.calls.map((c) => c[0].session_id)
    const inboxIds = api.inbox.mock.calls.map((c) => c[1])
    expect(heartbeatIds).not.toContain('session-archived')
    expect(inboxIds).not.toContain('session-archived')
  })
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}
const options = () => ({ surface: 'user' as const, getSessionId: () => 'session-stop', getTitle: () => '停止复测',
  isBusy: () => false, onMessage: vi.fn().mockResolvedValue(true) })

describe('Mesh退出与重启的异步边界', () => {
  beforeEach(() => {
    api.heartbeat.mockReset().mockResolvedValue({})
    api.inbox.mockReset().mockResolvedValue([])
  })

  it('心跳等待时停止，不再发后续会话心跳或收件箱请求', async () => {
    const pending = deferred<object>()
    api.heartbeat.mockReturnValueOnce(pending.promise)
    const bridge = createAgentMeshBridge({ ...options(), getSessions: () => [{ id: 'one', title: '一' }, { id: 'two', title: '二' }] })
    const sync = bridge.syncNow()
    bridge.stop()
    pending.resolve({})
    await sync
    await bridge.syncNow()
    expect(api.heartbeat).toHaveBeenCalledTimes(1)
    expect(api.inbox).not.toHaveBeenCalled()
  })

  it.each(['delivered', 'gone'])('收件箱%s返回前停止，不处理消息，也不触发会话失效回调', async (result) => {
    const pending = deferred<Array<{ message_id: string; status: string }>>()
    api.inbox.mockReturnValueOnce(pending.promise)
    const opts = options()
    const onSessionGone = vi.fn()
    const bridge = createAgentMeshBridge({ ...opts, onSessionGone })
    const sync = bridge.syncNow()
    await Promise.resolve()
    bridge.stop()
    if (result === 'gone') pending.reject({ code: 40321 })
    else pending.resolve([{ message_id: 'old', status: 'delivered' }])
    await sync
    expect(opts.onMessage).not.toHaveBeenCalled()
    expect(onSessionGone).not.toHaveBeenCalled()
  })

  it('停止后立即重新启动，旧响应不能解锁新同步或发旧请求', async () => {
    const oldHeartbeat = deferred<object>()
    const newHeartbeat = deferred<object>()
    api.heartbeat.mockReturnValueOnce(oldHeartbeat.promise).mockReturnValueOnce(newHeartbeat.promise)
    const bridge = createAgentMeshBridge(options())
    const oldSync = bridge.syncNow()
    bridge.stop()
    bridge.start()
    try {
      expect(api.heartbeat).toHaveBeenCalledTimes(2)
      oldHeartbeat.resolve({})
      await oldSync
      await bridge.syncNow()
      expect(api.heartbeat).toHaveBeenCalledTimes(2)
      expect(api.inbox).not.toHaveBeenCalled()
      newHeartbeat.resolve({})
      await Promise.resolve()
      await Promise.resolve()
      expect(api.inbox).toHaveBeenCalledOnce()
    } finally { bridge.stop() }
  })
})
