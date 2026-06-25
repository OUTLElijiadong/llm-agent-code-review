/**
 * WebSocket 讨论流客户端 (v2.3 M7 / v2.4 心跳增强)
 *
 * URL 构建:
 *   dev 环境: ws://localhost:5173/api/ws/discuss/{session_id}  → Vite 代理至 localhost:8000
 *   prod 环境: wss://domain/api/ws/discuss/{session_id}        → nginx 代理至后端
 *
 * 心跳机制 (v2.4):
 *   - 每 30 秒发送 {"action":"ping"},后端响应 {"type":"pong"}
 *   - 连续 3 次未收到 pong 视为连接断开,主动 close 触发重连
 *   - 防止 nginx proxy_read_timeout(3600s) 内的中间网络设备因空闲断连
 *   - 与 nginx proxy_socket_keepalive(TCP 层) 双重保活
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

/** 心跳间隔(毫秒) - 30 秒发送一次 ping */
const HEARTBEAT_INTERVAL = 30_000
/** 最大未响应次数 - 连续 3 次未收到 pong 认为连接断开 */
const MAX_MISSED_PONGS = 3

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

  // 心跳状态变量
  let heartbeatTimer: ReturnType<typeof setTimeout> | null = null
  let missedPongs = 0
  let waitingForPong = false

  /**
   * 启动心跳定时器
   * 每 HEARTBEAT_INTERVAL 毫秒发送一次 ping,若上一轮 ping 未收到 pong 则累加 missedPongs。
   */
  function startHeartbeat() {
    stopHeartbeat()
    missedPongs = 0
    waitingForPong = false
    heartbeatTimer = setInterval(() => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return

      if (waitingForPong) {
        // 上一轮 ping 未收到 pong,累加未响应计数
        missedPongs++
        opts.onError?.(`WebSocket 心跳未响应 (${missedPongs}/${MAX_MISSED_PONGS})`)
        if (missedPongs >= MAX_MISSED_PONGS) {
          // 连续多次未响应,主动断开触发重连
          opts.onError?.('WebSocket 心跳超时,主动断开重连')
          ws.close()
          return
        }
      }

      // 发送 ping
      try {
        ws.send(JSON.stringify({ action: 'ping' }))
        waitingForPong = true
      } catch {
        // 发送失败,连接可能已断开
        opts.onError?.('WebSocket 心跳发送失败')
        ws.close()
      }
    }, HEARTBEAT_INTERVAL)
  }

  /**
   * 停止心跳定时器
   */
  function stopHeartbeat() {
    if (heartbeatTimer) {
      clearInterval(heartbeatTimer)
      heartbeatTimer = null
    }
    waitingForPong = false
    missedPongs = 0
  }

  function connect() {
    if (closed) return
    opts.onStatus?.('connecting')
    const protocols = buildProtocols()
    ws = protocols ? new WebSocket(url, protocols) : new WebSocket(url)

    ws.onopen = () => {
      opts.onStatus?.('connected')
      backoff = 2000
      // 连接建立后启动心跳
      startHeartbeat()
    }

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data) as WsMessage
        // 收到 pong,重置心跳未响应计数
        if (msg.type === 'pong') {
          waitingForPong = false
          missedPongs = 0
          return
        }
        onMessage(msg)
      } catch {
        // ignore malformed frame
      }
    }

    ws.onclose = () => {
      if (closed) return
      // 连接关闭时停止心跳
      stopHeartbeat()
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
      stopHeartbeat()
      if (reconnectTimer) clearTimeout(reconnectTimer)
      ws?.close()
    },
  }
}
