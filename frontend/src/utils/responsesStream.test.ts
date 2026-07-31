import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ResponseStreamEvent } from '@/types/responses'
import {
  ResponsesStreamHttpError,
  ResponsesStreamProtocolError,
  streamResponses,
} from '@/utils/responsesStream'
import { setToken } from '@/utils/token'

function streamResponse(chunks: string[]): Response {
  const encoder = new TextEncoder()
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
  return new Response(stream, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  })
}

describe('streamResponses', () => {
  beforeEach(() => {
    setToken('responses-token')
  })

  it('parses split CRLF frames, multiline data and keepalive comments', async () => {
    const response = streamResponse([
      ': keep-alive\r',
      '\n\r\nevent: response.created\r\ndata: {"type":"response.created",\r\n',
      'data: "response":{"id":"resp-1"}}\r\n\r\n',
      'event: response.output_item.added\ndata: {"type":"response.output_item.added","output_index":0,"item":{"id":"item-1"}}\n\n',
      'data: {"type":"response.completed","response":{"id":"resp-1","status":"completed"}}\n\n',
    ])
    const fetchMock = vi.fn().mockResolvedValue(response)
    vi.stubGlobal('fetch', fetchMock)
    const events: ResponseStreamEvent[] = []

    const handle = streamResponses({ input: '检查代码' }, { onEvent: (event) => events.push(event) })
    await handle.done

    expect(fetchMock).toHaveBeenCalledWith('/api/agent-responses/stream', expect.objectContaining({
      method: 'POST',
      headers: {
        Accept: 'text/event-stream',
        Authorization: 'Bearer responses-token',
        'Content-Type': 'application/json',
      },
      credentials: 'same-origin',
      body: JSON.stringify({ input: '检查代码' }),
      signal: handle.signal,
    }))
    expect(events.map((event) => event.type)).toEqual([
      'response.created',
      'response.output_item.added',
      'response.completed',
    ])
    expect(events[0]).toMatchObject({ response: { id: 'resp-1' } })
  })

  it('preserves meaningful output newlines, drops empty deltas and completes without DONE', async () => {
    const frames = [
      'data: {"type":"response.output_text.delta","delta":""}\n\n',
      'data: {"type":"response.output_text.delta","delta":"\\n\\n"}\n\n',
      'data: {"type":"response.output_text.delta","delta":"const value = 1\\n"}\n\n',
      'data: {"type":"response.function_call_arguments.delta","delta":""}\n\n',
      'data: {"type":"response.completed","response":{"id":"resp-2","status":"completed"}}\n\n',
    ]
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResponse(frames)))
    const events: ResponseStreamEvent[] = []

    await streamResponses({}, { onEvent: (event) => events.push(event) }).done

    expect(events.map((event) => event.type)).toEqual([
      'response.output_text.delta',
      'response.output_text.delta',
      'response.completed',
    ])
    expect(events[0]).toMatchObject({ delta: '\n\n' })
    expect(events[1]).toMatchObject({ delta: 'const value = 1\n' })
  })

  it('routes function, approval, input, terminal and protocol error events', async () => {
    const eventPayloads = [
      { type: 'response.function_call_arguments.delta', item_id: 'item-1', delta: '{"path"' },
      { type: 'response.function_call_arguments.done', item_id: 'item-1', arguments: '{"path":"a.ts"}' },
      {
        type: 'response.approval.required', run_id: 'run-1', call_id: 'call-1',
        tool_name: 'run_terminal', arguments: { command: 'npm test' }, operation: '执行测试',
        impact: '运行项目测试', danger: false,
      },
      { type: 'response.input.required', run_id: 'run-1', question: '选择目标分支' },
      { type: 'response.incomplete', response: { id: 'resp-3' } },
      { type: 'response.failed', response: { id: 'resp-4' } },
      { type: 'error', message: '上游失败' },
    ]
    const frames = eventPayloads.map((payload) => `data: ${JSON.stringify(payload)}\n\n`)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResponse(frames)))
    const events: ResponseStreamEvent[] = []

    await streamResponses({}, { onEvent: (event) => events.push(event) }).done

    expect(events).toHaveLength(eventPayloads.length)
    expect(events.map((event) => event.type)).toEqual(eventPayloads.map((event) => event.type))
    expect(events[2]).toMatchObject({
      run_id: 'run-1', call_id: 'call-1', tool_name: 'run_terminal', danger: false,
    })
  })

  it('rejects a clean EOF that has no terminal or explicit pause event', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResponse([
      'data: {"type":"response.created","response":{"id":"resp-cut"}}\n\n',
      'data: {"type":"response.output_text.delta","delta":"部分内容"}\n\n',
    ])))
    const onError = vi.fn()

    const error = await streamResponses({}, { onEvent: vi.fn(), onError }).done
      .catch((reason: unknown) => reason)

    expect(error).toBeInstanceOf(ResponsesStreamProtocolError)
    expect(error).toMatchObject({ message: 'Responses 流在明确终态之前中断' })
    expect(onError).toHaveBeenCalledWith(error)
  })

  it('accepts approval and input events as explicit pause boundaries', async () => {
    const pauseEvents = [
      {
        type: 'response.approval.required', run_id: 'run-pause', call_id: 'call-1',
        tool_name: 'admin_delete_user', arguments: { user_id: 9 }, operation: '删除用户',
        impact: '删除账号', danger: true,
      },
      { type: 'response.input.required', run_id: 'run-pause', call_id: 'call-2', question: '确认目标' },
    ]
    for (const pauseEvent of pauseEvents) {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResponse([
        `data: ${JSON.stringify(pauseEvent)}\n\n`,
      ])))
      await expect(streamResponses({}, { onEvent: vi.fn() }).done).resolves.toBeUndefined()
    }
  })

  it('accepts a cancelled response as an explicit terminal boundary', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResponse([
      'data: {"type":"response.cancelled","response":{"id":"resp-cancelled","status":"cancelled"}}\n\n',
    ])))
    const events: ResponseStreamEvent[] = []

    await expect(streamResponses({}, { onEvent: (event) => events.push(event) }).done)
      .resolves.toBeUndefined()
    expect(events).toEqual([
      { type: 'response.cancelled', response: { id: 'resp-cancelled', status: 'cancelled' } },
    ])
  })

  it('passes an ephemeral sensitive result through before the terminal event', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResponse([
      'data: {"type":"response.sensitive.result","run_id":"run-secret","call_id":"call-secret","capability":"beta_codes.generate","title":"内测码","notice":"刷新后不可恢复","values":["BETA-ONCE"]}\n\n',
      'data: {"type":"response.completed","response":{"id":"run-secret","status":"completed"}}\n\n',
    ])))
    const events: ResponseStreamEvent[] = []

    await streamResponses({}, { onEvent: (event) => events.push(event) }).done

    expect(events).toHaveLength(2)
    expect(events[0]).toMatchObject({
      type: 'response.sensitive.result',
      capability: 'beta_codes.generate',
      values: ['BETA-ONCE'],
    })
  })

  it('uses a JSON message for HTTP errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ message: '请求参数不完整' }),
      { status: 422, headers: { 'Content-Type': 'application/json' } },
    )))
    const onError = vi.fn()

    const handle = streamResponses({}, { onEvent: vi.fn(), onError })
    const error = await handle.done.catch((reason: unknown) => reason)
    expect(error).toBeInstanceOf(ResponsesStreamHttpError)
    expect(error).toMatchObject({
      name: 'ResponsesStreamHttpError',
      message: '请求参数不完整',
      status: 422,
    })
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ message: '请求参数不完整' }))
  })

  it('aborts the active request without reporting an operational error', async () => {
    const onError = vi.fn()
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => {
        reject(new DOMException('aborted', 'AbortError'))
      }, { once: true })
    }))
    vi.stubGlobal('fetch', fetchMock)

    const handle = streamResponses({}, { onEvent: vi.fn(), onError })
    handle.abort()

    await expect(handle.done).rejects.toMatchObject({ name: 'AbortError' })
    expect(handle.signal.aborted).toBe(true)
    expect(onError).not.toHaveBeenCalled()
  })
})
