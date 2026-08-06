import type {
  ResponsesStreamHandle,
  ResponsesStreamOptions,
  ResponseStreamEvent,
  ResponseStreamEventType,
} from '@/types/responses'
import { getToken } from '@/utils/token'

const DEFAULT_ENDPOINT = '/api/agent-responses/stream'

const SUPPORTED_EVENTS = new Set<ResponseStreamEventType>([
  'response.created',
  'response.output_text.delta',
  'response.output_item.added',
  'response.output_item.done',
  'response.function_call_arguments.delta',
  'response.function_call_arguments.done',
  'response.tool.started',
  'response.tool.completed',
  'response.tool.failed',
  'response.approval.required',
  'response.input.required',
  'response.sensitive.result',
  'response.completed',
  'response.incomplete',
  'response.failed',
  'response.cancelled',
  'auth_expired',
  'error',
])

export class ResponsesStreamHttpError extends Error {
  readonly status: number
  readonly payload: unknown

  constructor(message: string, status: number, payload: unknown) {
    super(message)
    this.name = 'ResponsesStreamHttpError'
    this.status = status
    this.payload = payload
  }
}

export class ResponsesStreamProtocolError extends Error {
  constructor(message = 'Responses 流在明确终态之前中断') {
    super(message)
    this.name = 'ResponsesStreamProtocolError'
  }
}

function isCompletionBoundary(event: ResponseStreamEvent): boolean {
  return event.type === 'response.completed'
    || event.type === 'response.incomplete'
    || event.type === 'response.failed'
    || event.type === 'response.cancelled'
    || event.type === 'response.approval.required'
    || event.type === 'response.input.required'
    || event.type === 'auth_expired'
    || event.type === 'error'
}

class SseParser {
  private buffer = ''
  private eventName = ''
  private dataLines: string[] = []

  constructor(private readonly emit: (event: ResponseStreamEvent) => void) {}

  push(chunk: string): void {
    this.buffer += chunk
    this.drainLines(false)
  }

  finish(): void {
    this.drainLines(true)
    if (this.buffer.length > 0) {
      this.consumeLine(this.buffer)
      this.buffer = ''
    }
    this.dispatch()
  }

  private drainLines(final: boolean): void {
    while (true) {
      const match = /[\r\n]/.exec(this.buffer)
      if (!match) return

      const index = match.index
      const marker = this.buffer[index]
      if (marker === '\r' && index === this.buffer.length - 1 && !final) return

      const terminatorLength = marker === '\r' && this.buffer[index + 1] === '\n' ? 2 : 1
      const line = this.buffer.slice(0, index)
      this.buffer = this.buffer.slice(index + terminatorLength)
      this.consumeLine(line)
    }
  }

  private consumeLine(line: string): void {
    if (line === '') {
      this.dispatch()
      return
    }
    if (line.startsWith(':')) return

    const colon = line.indexOf(':')
    const field = colon < 0 ? line : line.slice(0, colon)
    let value = colon < 0 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)

    if (field === 'event') this.eventName = value
    if (field === 'data') this.dataLines.push(value)
  }

  private dispatch(): void {
    if (this.dataLines.length === 0) {
      this.eventName = ''
      return
    }

    const rawData = this.dataLines.join('\n')
    const eventName = this.eventName
    this.eventName = ''
    this.dataLines = []

    // 兼容其他供应商,但完成判断从不依赖这个哨兵。
    if (rawData === '[DONE]') return

    const parsed = JSON.parse(rawData) as Record<string, unknown>
    const type = typeof parsed.type === 'string' ? parsed.type : eventName
    if (!SUPPORTED_EVENTS.has(type as ResponseStreamEventType)) return

    const event = { ...parsed, type } as ResponseStreamEvent
    if (event.type === 'auth_expired') {
      window.dispatchEvent(new Event('prism:auth-expired'))
    }
    if (
      (event.type === 'response.output_text.delta'
        || event.type === 'response.function_call_arguments.delta')
      && event.delta.length === 0
    ) {
      return
    }
    this.emit(event)
  }
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
    || error instanceof Error && error.name === 'AbortError'
}

function extractErrorMessage(payload: unknown, status: number): string {
  if (payload && typeof payload === 'object') {
    const body = payload as Record<string, unknown>
    if (typeof body.message === 'string' && body.message.trim()) return body.message
    if (typeof body.detail === 'string' && body.detail.trim()) return body.detail
    if (body.error && typeof body.error === 'object') {
      const message = (body.error as Record<string, unknown>).message
      if (typeof message === 'string' && message.trim()) return message
    }
  }
  return `Responses 请求失败 (${status})`
}

async function readHttpError(response: Response): Promise<ResponsesStreamHttpError> {
  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    payload = null
  }
  return new ResponsesStreamHttpError(
    extractErrorMessage(payload, response.status),
    response.status,
    payload,
  )
}

/**
 * 发起一次 Agent Responses SSE 请求。事件完成不依赖 `[DONE]`,以服务端终止流为准。
 */
export function streamResponses(
  body: unknown,
  options: ResponsesStreamOptions,
): ResponsesStreamHandle {
  const controller = new AbortController()
  const externalSignal = options.signal

  const abortFromExternal = () => controller.abort(externalSignal?.reason)
  if (externalSignal?.aborted) abortFromExternal()
  else externalSignal?.addEventListener('abort', abortFromExternal, { once: true })

  const done = (async (): Promise<void> => {
    const token = getToken()
    if (!token) throw new Error('登录状态已失效,无法发起 Agent 请求')

    const response = await fetch(options.endpoint ?? DEFAULT_ENDPOINT, {
      method: 'POST',
      headers: {
        Accept: 'text/event-stream',
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      credentials: 'same-origin',
      body: JSON.stringify(body),
      signal: controller.signal,
    })

    if (!response.ok) throw await readHttpError(response)
    if (!response.body) throw new Error('Responses 流响应缺少可读内容')

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let hasCompletionBoundary = false
    const parser = new SseParser((event) => {
      if (isCompletionBoundary(event)) {
        // 每个流只派发一次:小菱完成任务,让相关页面刷新数据
        if (!hasCompletionBoundary) {
          hasCompletionBoundary = true
          window.dispatchEvent(new CustomEvent('prism:agent-task-complete', {
            detail: { surface: (body as Record<string, unknown>).surface },
          }))
        }
      }
      options.onEvent(event)
    })

    while (true) {
      const { done: streamDone, value } = await reader.read()
      if (streamDone) break
      parser.push(decoder.decode(value, { stream: true }))
    }
    parser.push(decoder.decode())
    parser.finish()
    if (!hasCompletionBoundary) throw new ResponsesStreamProtocolError()
  })()
    .catch((error: unknown) => {
      if (!isAbortError(error)) options.onError?.(error)
      throw error
    })
    .finally(() => {
      externalSignal?.removeEventListener('abort', abortFromExternal)
    })

  return {
    abort: () => controller.abort(),
    signal: controller.signal,
    done,
  }
}
