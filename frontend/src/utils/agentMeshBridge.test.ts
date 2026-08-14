import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  heartbeat: vi.fn(),
  inbox: vi.fn(),
}))

vi.mock('@/api/agentMesh', () => ({
  heartbeatAgentMesh: api.heartbeat,
  pullAgentMeshInbox: api.inbox,
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
})
