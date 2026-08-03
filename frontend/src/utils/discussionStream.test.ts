import { expect, it, vi } from 'vitest'

import { subscribeDiscussion } from '@/utils/discussionStream'
import { setToken } from '@/utils/token'

class FakeWebSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  static instances: FakeWebSocket[] = []

  readonly url: string
  readonly protocols?: string | string[]
  readyState = FakeWebSocket.CONNECTING
  sent: string[] = []
  closeCalls = 0
  throwOnSend = false
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null

  /**
   * 记录 WebSocket 构造参数并注册实例。
   * @param url - 连接地址。
   * @param protocols - 可选鉴权子协议。
   */
  constructor(url: string | URL, protocols?: string | string[]) {
    this.url = String(url)
    this.protocols = protocols
    FakeWebSocket.instances.push(this)
  }

  /**
   * 重置全部 fake 连接记录。
   * @returns 无返回值，仅清理类级状态。
   */
  static reset(): void {
    FakeWebSocket.instances = []
  }

  /**
   * 记录发送内容或模拟浏览器发送失败。
   * @param data - 待发送字符串。
   */
  send(data: string): void {
    if (this.throwOnSend) throw new Error('send failed')
    this.sent.push(data)
  }

  /**
   * 主动关闭连接并触发 close 回调。
   * @param code - 关闭码。
   * @param reason - 关闭原因。
   */
  close(code: number = 1000, reason: string = ''): void {
    if (this.readyState === FakeWebSocket.CLOSED) return
    this.closeCalls += 1
    this.readyState = FakeWebSocket.CLOSED
    this.onclose?.({ code, reason } as CloseEvent)
  }

  /**
   * 模拟连接成功。
   * @returns 无返回值，仅触发 open 回调。
   */
  open(): void {
    this.readyState = FakeWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  /**
   * 模拟收到服务端文本帧。
   * @param data - 服务端消息文本。
   */
  receive(data: string): void {
    this.onmessage?.({ data } as MessageEvent)
  }

  /**
   * 模拟服务端关闭连接。
   * @param code - 服务端关闭码。
   * @param reason - 服务端关闭原因。
   */
  serverClose(code: number, reason: string = ''): void {
    this.readyState = FakeWebSocket.CLOSED
    this.onclose?.({ code, reason } as CloseEvent)
  }

  /**
   * 模拟 WebSocket error 事件。
   * @returns 无返回值，仅触发错误回调。
   */
  fail(): void {
    this.onerror?.(new Event('error'))
  }
}

/**
 * 安装 fake WebSocket 构造器并清理旧实例。
 * @returns 无返回值，仅更新全局 WebSocket。
 */
function installFakeWebSocket(): void {
  FakeWebSocket.reset()
  vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket)
}

/** 验证 URL、鉴权协议、业务消息过滤、发送和主动关闭。 */
it('connects with token protocols and routes only business messages', function testConnectionAndMessages(): void {
  installFakeWebSocket()
  setToken('ws-token')
  const onMessage = vi.fn()
  const onStatus = vi.fn()
  const onError = vi.fn()

  const stream = subscribeDiscussion('session-1', onMessage, {
    wsUrl: '/custom/discuss/session-1',
    onStatus,
    onError,
  })
  const socket = FakeWebSocket.instances[0]

  expect(socket.url).toBe('wss://review.example/custom/discuss/session-1')
  expect(socket.protocols).toEqual(['prism-auth', 'ws-token'])
  expect(onStatus).toHaveBeenCalledWith('connecting')
  stream.send('pause')
  expect(socket.sent).toEqual([])

  socket.open()
  expect(onStatus).toHaveBeenLastCalledWith('connected')
  stream.send('user_input', { content: '检查权限' })
  expect(JSON.parse(socket.sent[0])).toEqual({ action: 'user_input', content: '检查权限' })

  socket.receive(JSON.stringify({ type: 'pong' }))
  socket.receive(JSON.stringify({ type: 'server_ping', ts: 1 }))
  socket.receive('not-json')
  socket.receive(JSON.stringify({ type: 'session_end' }))
  expect(onMessage).toHaveBeenCalledTimes(1)
  expect(onMessage).toHaveBeenCalledWith({ type: 'session_end' })

  socket.fail()
  expect(onError).not.toHaveBeenCalled()
  stream.close()
  expect(socket.closeCalls).toBe(1)
})

/** 验证业务终态主动关闭后不会再报网络错误或安排重连。 */
it.each([
  ['session_end', { type: 'session_end' }],
  ['control done', { type: 'control', session_id: 'terminal-session', action: 'done', payload: {} }],
])('stops permanently after %s', async (_label, terminalFrame): Promise<void> => {
  vi.useFakeTimers()
  installFakeWebSocket()
  const onMessage = vi.fn()
  const onStatus = vi.fn()
  const onError = vi.fn()
  subscribeDiscussion('terminal-session', onMessage, { onStatus, onError })
  const socket = FakeWebSocket.instances[0]
  socket.open()

  socket.receive(JSON.stringify(terminalFrame))
  socket.fail()
  socket.serverClose(1006, 'closed after terminal frame')
  await vi.advanceTimersByTimeAsync(60_000)

  expect(onMessage).toHaveBeenCalledWith(terminalFrame)
  expect(socket.closeCalls).toBe(1)
  expect(FakeWebSocket.instances).toHaveLength(1)
  expect(onError).not.toHaveBeenCalled()
  expect(onStatus).not.toHaveBeenCalledWith('disconnected')
})

/** 验证无 token 的缺省 URL 以及 pong、超时和发送失败心跳分支。 */
it('maintains heartbeat state and reconnects after heartbeat failures', async function testHeartbeat(): Promise<void> {
  vi.useFakeTimers()
  installFakeWebSocket()
  const onError = vi.fn()
  const onStatus = vi.fn()
  const stream = subscribeDiscussion('heartbeat-session', vi.fn(), { onError, onStatus })
  const socket = FakeWebSocket.instances[0]

  expect(socket.url).toBe('wss://review.example/api/ws/discuss/heartbeat-session')
  expect(socket.protocols).toBeUndefined()
  socket.open()

  await vi.advanceTimersByTimeAsync(30_000)
  expect(JSON.parse(socket.sent[0])).toEqual({ action: 'ping' })
  socket.receive(JSON.stringify({ type: 'pong' }))
  await vi.advanceTimersByTimeAsync(30_000)
  expect(onError).not.toHaveBeenCalledWith(expect.stringContaining('心跳未响应'))

  await vi.advanceTimersByTimeAsync(30_000)
  await vi.advanceTimersByTimeAsync(30_000)
  await vi.advanceTimersByTimeAsync(30_000)
  expect(onError).toHaveBeenCalledWith('WebSocket 心跳未响应 (3/3)')
  expect(onError).toHaveBeenCalledWith('WebSocket 心跳超时,主动断开重连')
  expect(onStatus).toHaveBeenCalledWith('disconnected')
  expect(socket.closeCalls).toBe(1)

  await vi.advanceTimersByTimeAsync(3_000)
  const reconnect = FakeWebSocket.instances[1]
  reconnect.throwOnSend = true
  reconnect.open()
  await vi.advanceTimersByTimeAsync(30_000)
  expect(onError).toHaveBeenCalledWith('WebSocket 心跳发送失败')
  expect(reconnect.closeCalls).toBe(1)

  stream.close()
})

/** 验证普通断线指数重连、致命关闭码与最大重试上限。 */
it('reconnects transient failures but stops on fatal or repeated closes', async function testReconnectLimits(): Promise<void> {
  vi.useFakeTimers()
  installFakeWebSocket()
  const transientStatus = vi.fn()
  const transientStream = subscribeDiscussion('retry-session', vi.fn(), {
    onStatus: transientStatus,
  })
  const first = FakeWebSocket.instances[0]
  first.serverClose(1006)
  expect(transientStatus).toHaveBeenLastCalledWith('disconnected')
  await vi.advanceTimersByTimeAsync(2_999)
  expect(FakeWebSocket.instances).toHaveLength(1)
  await vi.advanceTimersByTimeAsync(1)
  expect(FakeWebSocket.instances).toHaveLength(2)
  FakeWebSocket.instances[1].open()
  expect(transientStatus).toHaveBeenLastCalledWith('connected')
  transientStream.close()

  installFakeWebSocket()
  const fatalError = vi.fn()
  const fatalStatus = vi.fn()
  const dispatch = vi.spyOn(window, 'dispatchEvent')
  subscribeDiscussion('fatal-session', vi.fn(), {
    onError: fatalError,
    onStatus: fatalStatus,
  })
  FakeWebSocket.instances[0].serverClose(4001, '账号已在另一台设备登录')
  expect(fatalError).toHaveBeenCalledWith('连接被服务端拒绝(4001): 账号已在另一台设备登录')
  expect(fatalStatus).toHaveBeenLastCalledWith('error')
  expect(dispatch).toHaveBeenCalledWith(
    expect.objectContaining({ type: 'prism:auth-expired' }),
  )
  await vi.runAllTimersAsync()
  expect(FakeWebSocket.instances).toHaveLength(1)

  installFakeWebSocket()
  const cappedError = vi.fn()
  const cappedStatus = vi.fn()
  subscribeDiscussion('capped-session', vi.fn(), {
    onError: cappedError,
    onStatus: cappedStatus,
  })
  for (let attempt = 1; attempt <= 11; attempt += 1) {
    const current = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]
    current.serverClose(1006)
    if (attempt <= 10) await vi.runOnlyPendingTimersAsync()
  }
  expect(cappedError).toHaveBeenCalledWith('多次重连失败,已停止自动重连;请刷新页面重试')
  expect(cappedStatus).toHaveBeenLastCalledWith('error')
})
