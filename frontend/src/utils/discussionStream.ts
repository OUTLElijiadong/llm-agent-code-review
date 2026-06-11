/**
 * WebSocket 讨论流客户端 (v2.3 M7)
 *
 * URL 构建:
 *   dev 环境: ws://localhost:5173/api/ws/discuss/{session_id}  → Vite 代理至 localhost:8000
 *   prod 环境: wss://domain/api/ws/discuss/{session_id}        → Caddy 代理至后端
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
  wsUrl?: string
  onStatus?: (status: 'connecting' | 'connected' | 'disconnected' | 'error') => void
  onError?: (message: string) => void
}

/**
 * 把后端返回的相对/绝对 ws_url 解析为当前页面可用的 WebSocket 地址。
 * @param sessionId - 讨论会话 ID,用于缺省路径兜底。
 * @param wsUrl - 后端预检返回的 WebSocket 路径或完整地址。
 * @returns WebSocket URL；鉴权 token 通过 Sec-WebSocket-Protocol 发送，避免出现在访问日志 URL 中。
 */
function buildWsUrl(sessionId: string, wsUrl?: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const fallback = `${proto}://${window.location.host}/api/ws/discuss/${sessionId}`
  const raw = wsUrl?.trim() || fallback
  const base = raw.startsWith('ws://') || raw.startsWith('wss://')
    ? raw
    : `${proto}://${window.location.host}${raw.startsWith('/') ? raw : `/${raw}`}`
  const url = new URL(base)
  return url.toString()
}

/**
 * 生成 WebSocket 子协议鉴权参数。
 * @returns 浏览器 WebSocket 构造器可接受的协议列表。
 */
function buildProtocols(): string[] | undefined {
  const token = getToken()
  return token ? ['prism-auth', token] : undefined
}

export function subscribeDiscussion(
  sessionId: string,
  onMessage: (msg: WsMessage) => void,
  opts: SubOpts = {},
): DiscussionStream {
  const url = buildWsUrl(sessionId, opts.wsUrl)
  let ws: WebSocket | null = null
  let closed = false
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let backoff = 2000

  function connect() {
    if (closed) return
    opts.onStatus?.('connecting')
    const protocols = buildProtocols()
    ws = protocols ? new WebSocket(url, protocols) : new WebSocket(url)

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
