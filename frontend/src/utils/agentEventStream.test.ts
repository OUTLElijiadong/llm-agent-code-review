import { expect, it, vi } from 'vitest'

import { subscribeAgentEvents } from '@/utils/agentEventStream'
import { setToken } from '@/utils/token'

class SequenceReader {
  private readonly chunks: Uint8Array[]
  private index = 0

  /**
   * 创建按顺序返回 UTF-8 数据块的读取器。
   * @param frames - 需要模拟的 SSE 文本块。
   */
  constructor(frames: string[]) {
    const encoder = new TextEncoder()
    this.chunks = frames.map(function encodeFrame(frame: string): Uint8Array {
      return encoder.encode(frame)
    })
  }

  /**
   * 返回下一段 SSE 字节，耗尽后返回 done。
   * @returns ReadableStream 兼容的读取结果。
   */
  async read(): Promise<ReadableStreamReadResult<Uint8Array>> {
    if (this.index >= this.chunks.length) return { done: true, value: undefined }
    const value = this.chunks[this.index]
    this.index += 1
    return { done: false, value }
  }
}

class SequenceBody {
  private readonly frames: string[]

  /**
   * 保存响应流文本块。
   * @param frames - SSE 文本块。
   */
  constructor(frames: string[]) {
    this.frames = frames
  }

  /**
   * 创建一次性顺序读取器。
   * @returns 预设数据读取器。
   */
  getReader(): SequenceReader {
    return new SequenceReader(this.frames)
  }
}

/**
 * 构造满足被测代码所需字段的 fetch Response。
 * @param frames - SSE 响应文本块。
 * @returns 带有可读 body 的最小 Response。
 */
function createStreamResponse(frames: string[]): Response {
  return {
    ok: true,
    status: 200,
    body: new SequenceBody(frames),
  } as unknown as Response
}

/**
 * 连续让出微任务队列，等待异步读取循环完成。
 * @param rounds - 让出微任务的次数。
 * @returns 所有指定微任务轮次完成后的 Promise。
 */
async function flushMicrotasks(rounds: number = 12): Promise<void> {
  for (let index = 0; index < rounds; index += 1) {
    await Promise.resolve()
  }
}

/** 验证 SSE 鉴权请求、分块帧解析、事件过滤和断流重连。 */
it('parses agent SSE frames and schedules reconnect after an unexpected end', async function testSseParsing(): Promise<void> {
  vi.useFakeTimers()
  setToken('sse-token')
  const frames = [
    ':connected\n\nevent: other\ndata: {"ignored":true}\n\n',
    'event: agent\ndata: {"type":"thinking","agent":"reviewer",',
    '\ndata: "trace_id":"trace-1","message":"分析中","payload":{},"timestamp":"now"}\n\n',
    'event: agent\ndata: not-json\n\n',
  ]
  const fetchMock = vi.fn().mockResolvedValue(createStreamResponse(frames))
  const onEvent = vi.fn()
  const onError = vi.fn()
  const onStatus = vi.fn()
  const warn = vi.spyOn(console, 'warn').mockImplementation(function ignoreWarning(): void {})
  vi.stubGlobal('fetch', fetchMock)

  const stream = subscribeAgentEvents(onEvent, {
    baseUrl: '/custom-api',
    replay: 7,
    onError,
    onStatus,
  })
  await flushMicrotasks()

  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(fetchMock.mock.calls[0][0]).toBe('/custom-api/agents/events?replay=7')
  expect(fetchMock.mock.calls[0][1]).toMatchObject({
    method: 'GET',
    headers: { Authorization: 'Bearer sse-token' },
    credentials: 'same-origin',
  })
  expect(fetchMock.mock.calls[0][1].signal).toBeInstanceOf(AbortSignal)
  expect(onEvent).toHaveBeenCalledTimes(1)
  expect(onEvent.mock.calls[0][0]).toMatchObject({
    type: 'thinking',
    agent: 'reviewer',
    trace_id: 'trace-1',
  })
  // 坏帧被静默忽略(生产不刷 console),不再额外触发 onEvent;
  // 流意外结束仍会触发重连(下方 onStatus 断言验证)
  expect(warn).not.toHaveBeenCalled()
  expect(onStatus.mock.calls.map(function statusValue(call): unknown { return call[0] })).toEqual([
    'connecting',
    'connected',
    'reconnecting',
  ])
  expect(onError).toHaveBeenCalledTimes(1)
  expect(String(onError.mock.calls[0][0])).toContain('SSE 流意外结束')

  stream.close()
  expect(onStatus).toHaveBeenLastCalledWith('closed')
})

/** 单设备会话被替换时停止 SSE 重连并通知全局鉴权层。 */
it('closes the stream on auth_expired without reconnecting', async function testAuthExpired(): Promise<void> {
  vi.useFakeTimers()
  setToken('old-device-token')
  const fetchMock = vi.fn().mockResolvedValue(createStreamResponse([
    'event: auth_expired\ndata: {"code":40102}\n\n',
  ]))
  const onStatus = vi.fn()
  const dispatch = vi.spyOn(window, 'dispatchEvent')
  vi.stubGlobal('fetch', fetchMock)

  subscribeAgentEvents(vi.fn(), { onStatus })
  await flushMicrotasks()
  await vi.runAllTimersAsync()

  expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({ type: 'prism:auth-expired' }))
  expect(onStatus).toHaveBeenLastCalledWith('closed')
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

/** 验证缺少 token 时延迟请求，并在 token 就绪后处理 HTTP 失败。 */
it('waits for a token before connecting and reports an invalid response', async function testMissingTokenAndBadResponse(): Promise<void> {
  vi.useFakeTimers()
  const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 401, body: null })
  const onError = vi.fn()
  const onStatus = vi.fn()
  vi.stubGlobal('fetch', fetchMock)

  const stream = subscribeAgentEvents(vi.fn(), { onError, onStatus })
  expect(fetchMock).not.toHaveBeenCalled()
  expect(onStatus).toHaveBeenNthCalledWith(1, 'connecting')
  expect(onStatus).toHaveBeenNthCalledWith(2, 'reconnecting')

  setToken('late-token')
  await vi.advanceTimersByTimeAsync(5_000)
  await flushMicrotasks()

  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(onError).toHaveBeenCalledTimes(1)
  expect(String(onError.mock.calls[0][0])).toContain('SSE 返回 401')
  expect(onStatus).toHaveBeenLastCalledWith('reconnecting')

  stream.close()
  await vi.runAllTimersAsync()
  expect(fetchMock).toHaveBeenCalledTimes(1)
})

/** 验证 AbortError 与用户主动关闭不会触发错误回调或重连。 */
it('does not reconnect after an aborted request or explicit close', async function testAbortHandling(): Promise<void> {
  vi.useFakeTimers()
  setToken('abort-token')
  const abortError = Object.assign(new Error('aborted'), { name: 'AbortError' })
  const fetchMock = vi.fn().mockRejectedValue(abortError)
  const onError = vi.fn()
  const onStatus = vi.fn()
  vi.stubGlobal('fetch', fetchMock)

  const stream = subscribeAgentEvents(vi.fn(), { onError, onStatus })
  await flushMicrotasks()

  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(onError).not.toHaveBeenCalled()
  expect(onStatus).toHaveBeenCalledTimes(1)

  stream.close()
  await vi.runAllTimersAsync()
  expect(fetchMock).toHaveBeenCalledTimes(1)
  expect(onStatus).toHaveBeenLastCalledWith('closed')
})
