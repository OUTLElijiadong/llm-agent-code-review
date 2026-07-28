import { getToken } from '@/utils/token'
import type { AgentEvent } from '@/types/agentEvent'

/**
 * 订阅后端 SSE 事件流 (/api/agents/events)
 *
 * 使用 fetch + ReadableStream 而非原生 EventSource,因为后者不支持自定义 Header
 * (我们需要带 Authorization)。
 *
 * v2.0 A3: 内置指数退避自动重连(5s → 30s 上限),网络断开后不需要手动刷新页面。
 */
export interface AgentEventStream {
  close: () => void
}

interface SubscribeOptions {
  baseUrl?: string
  replay?: number
  onError?: (e: unknown) => void
  onStatus?: (status: 'connecting' | 'connected' | 'reconnecting' | 'closed') => void
}

const MIN_BACKOFF_MS = 5_000
const MAX_BACKOFF_MS = 30_000

export function subscribeAgentEvents(
  onEvent: (ev: AgentEvent) => void,
  options: SubscribeOptions = {},
): AgentEventStream {
  const base = options.baseUrl ?? import.meta.env.VITE_API_BASE_URL ?? '/api'
  const url = `${base}/agents/events?replay=${options.replay ?? 20}`
  const controller = new AbortController()
  let closedByUser = false
  let backoff = MIN_BACKOFF_MS
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null

  const flushFrame = (rawFrame: string) => {
    const lines = rawFrame.split('\n').map((l) => l.replace(/\r$/, ''))
    let eventName = ''
    const dataLines: string[] = []
    for (const line of lines) {
      if (line.startsWith(':')) continue
      if (line.startsWith('event:')) {
        eventName = line.slice(6).trim()
      } else if (line.startsWith('data:')) {
        dataLines.push(line.slice(5).trim())
      }
    }
    if (!dataLines.length) return
    if (eventName && eventName !== 'agent') return
    try {
      const ev: AgentEvent = JSON.parse(dataLines.join('\n'))
      onEvent(ev)
    } catch {
      // 单条事件解析失败不影响整个流,静默跳过(生产环境不刷 console)
    }
  }

  const connectOnce = async () => {
    if (closedByUser) return
    const token = getToken()
    // 无令牌时不发起请求(否则后端缺少 Authorization 会判成 400),稍后重连
    if (!token) {
      options.onStatus?.('connecting')
      scheduleReconnect()
      return
    }
    options.onStatus?.('connecting')
    try {
      const resp = await fetch(url, {
        method: 'GET',
        headers: { Authorization: `Bearer ${token}` },
        signal: controller.signal,
        credentials: 'same-origin',
      })
      if (!resp.ok || !resp.body) {
        throw new Error(`SSE 返回 ${resp.status}`)
      }
      options.onStatus?.('connected')
      backoff = MIN_BACKOFF_MS

      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        let idx = buffer.indexOf('\n\n')
        while (idx >= 0) {
          const frame = buffer.slice(0, idx)
          buffer = buffer.slice(idx + 2)
          flushFrame(frame)
          idx = buffer.indexOf('\n\n')
        }
      }
      throw new Error('SSE 流意外结束')
    } catch (err) {
      if (closedByUser || (err as Error)?.name === 'AbortError') return
      options.onError?.(err)
      scheduleReconnect()
    }
  }

  const scheduleReconnect = () => {
    if (closedByUser) return
    options.onStatus?.('reconnecting')
    const wait = backoff
    backoff = Math.min(MAX_BACKOFF_MS, Math.floor(backoff * 1.6))
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      connectOnce()
    }, wait)
  }

  void connectOnce()

  return {
    close: () => {
      closedByUser = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      controller.abort()
      options.onStatus?.('closed')
    },
  }
}
