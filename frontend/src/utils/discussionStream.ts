/**
 * WebSocket 讨论流客户端 (v2.3 M7)
 *
 * URL 构建:
 *   dev 环境: ws://localhost:5173/api/ws/discuss/{session_id}  → Vite 代理至 localhost:8000
 *   prod 环境: wss://domain/api/ws/discuss/{session_id}        → Nginx 代理至后端
 */
import { getToken } from '@/utils/token'

export interface DiscussionTurn {
  turn_id: number
  agent_code: string
  agent_name: string
  role: 'agent' | 'user'
  content: string
  timestamp: string
}

export type WsMessage =
  | { type: 'discuss'; session_id: string; turn: DiscussionTurn }
  | { type: 'control'; session_id: string; action: string; payload: Record<string, unknown> }
  | { type: 'session_end' }
  | { type: 'pong' }

export interface DiscussionStream {
  send: (action: string, payload?: Record<string, unknown>) => void
  close: () => void
}

interface SubOpts {
  onStatus?: (status: 'connecting' | 'connected' | 'disconnected' | 'error') => void
  onError?: (message: string) => void
}

function buildWsUrl(sessionId: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const host = window.location.host
  const token = getToken()
  return `${proto}://${host}/api/ws/discuss/${sessionId}?token=${token ?? ''}`
}

export function subscribeDiscussion(
  sessionId: string,
  onMessage: (msg: WsMessage) => void,
  opts: SubOpts = {},
): DiscussionStream {
  const url = buildWsUrl(sessionId)
  let ws: WebSocket | null = null
  let closed = false
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let backoff = 2000

  function connect() {
    if (closed) return
    opts.onStatus?.('connecting')
    ws = new WebSocket(url)

    ws.onopen = () => {
      opts.onStatus?.('connected')
      backoff = 2000
    }

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data) as WsMessage
        onMessage(msg)
      } catch {
        // ignore malformed frame
      }
    }

    ws.onclose = () => {
      if (closed) return
      opts.onStatus?.('disconnected')
      scheduleReconnect()
    }

    ws.onerror = () => {
      opts.onError?.('WebSocket 连接失败,正在尝试重连...')
      // onclose 会自动触发
    }
  }

  function scheduleReconnect() {
    if (closed) return
    backoff = Math.min(30000, backoff * 1.5)
    reconnectTimer = setTimeout(connect, backoff)
  }

  function send(action: string, payload?: Record<string, unknown>) {
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ action, ...(payload || {}) }))
    }
  }

  connect()

  return {
    send,
    close: () => {
      closed = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      ws?.close()
    },
  }
}
