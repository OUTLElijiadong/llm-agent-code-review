import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cancelAgentResponseRun } from './agentResponses'
import { setToken } from '@/utils/token'

vi.mock('@/api/http', () => ({ get: vi.fn() }))

function sse(events: unknown[]): Response {
  return new Response(new ReadableStream({
    start(controller) {
      for (const event of events) controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`))
      controller.close()
    },
  }), { headers: { 'Content-Type': 'text/event-stream' } })
}

beforeEach(() => setToken('cancel-test-token'))
afterEach(() => vi.unstubAllGlobals())

describe('取消请求以服务端SSE终态确认', () => {
  it.each(['completed', 'cancelled', 'failed', 'incomplete'] as const)('返回真实%s而非仅接受HTTP200', async (status) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sse([
      { type: 'response.created', response: { id: 'run-1', status: 'in_progress' } },
      { type: `response.${status}`, response: { id: 'run-1', status, output_text: '服务端结果', error: { message: '失败原因' } } },
    ])))
    await expect(cancelAgentResponseRun('user', 'session-1', 'run-1', '原因')).resolves.toMatchObject({
      run_id: 'run-1', status, output_text: '服务端结果', error: '失败原因',
    })
  })

  it('HTTP200但终态前断流必须拒绝确认取消', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sse([{ type: 'response.created', response: { id: 'run-1' } }])))
    await expect(cancelAgentResponseRun('user', 'session-1', 'run-1')).rejects.toThrow('终态')
  })

  it('服务端取消执行错误不能被随后failed帧当作取消成功', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sse([
      { type: 'error', error: { message: '检查点不可用' } },
      { type: 'response.failed', response: { id: 'run-1', status: 'failed' } },
    ])))
    await expect(cancelAgentResponseRun('user', 'session-1', 'run-1')).rejects.toThrow('检查点不可用')
  })

  it('其他运行的终态不确认本次取消', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sse([{ type: 'response.cancelled', response: { id: 'other', status: 'cancelled' } }])))
    await expect(cancelAgentResponseRun('user', 'session-1', 'run-1')).rejects.toThrow('停止请求未确认')
  })
})
