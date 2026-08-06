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
  action?: 'speak' | 'silent'
  stance?: 'propose' | 'agree' | 'oppose' | 'question' | 'supplement' | 'neutral'
  reply_to?: string | null
  round_index?: number
  timestamp: string
}

export type WsMessage =
  | { type: 'discuss'; session_id: string; turn: DiscussionTurn }
  | { type: 'control'; session_id: string; action: string; payload: Record<string, unknown> }
  | { type: 'session_end' }
  | { type: 'pong' }
  | { type: 'server_ping'; ts: number }

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
 * 连续重连失败上限 - 超过后停止重连并置 error 状态,由用户刷新页面重试。
 * 鉴权被拒/会话不存在时后端在 accept 前 close,浏览器只能看到 1006,
 * 无法从 close code 区分"网络抖动"与"永远不会成功",故用次数上限兜底。
 */
const MAX_RECONNECT_ATTEMPTS = 10
/** 服务端 accept 后主动拒绝的应用级关闭码(token 无效/无权/会话不存在) - 重连必然再被拒,直接放弃 */
const FATAL_CLOSE_CODES = new Set([4001, 4003, 4004])

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
  let reconnectAttempts = 0

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

  // 业务终态不是网络异常。终态帧交付给页面后立即停止心跳和自动重连，
  // 否则会话已经结束却在下一次断线时再次显示“连接失败”。
  function terminateForBusiness() {
    if (closed) return
    closed = true
    stopHeartbeat()
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    ws?.close()
  }

  function connect() {
    if (closed) return
    opts.onStatus?.('connecting')
    const protocols = buildProtocols()
    ws = protocols ? new WebSocket(url, protocols) : new WebSocket(url)

    ws.onopen = () => {
      opts.onStatus?.('connected')
      backoff = 2000
      reconnectAttempts = 0
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
        // 服务端保活探测帧,仅用于刷新 NAT 会话,不进业务回调
        if (msg.type === 'server_ping') return
        onMessage(msg)
        if (msg.type === 'session_end' || (msg.type === 'control' && msg.action === 'done')) {
          terminateForBusiness()
        }
      } catch {
        // ignore malformed frame
      }
    }

    ws.onclose = (event) => {
      if (closed) return
      // 连接关闭时停止心跳
      stopHeartbeat()
      if (FATAL_CLOSE_CODES.has(event.code)) {
        closed = true
        opts.onError?.(`连接被服务端拒绝(${event.code}): ${event.reason || '鉴权失败或会话不存在'}`)
        opts.onStatus?.('error')
        return
      }
      opts.onStatus?.('disconnected')
      scheduleReconnect()
    }

    ws.onerror = () => {
      if (closed) return
      opts.onError?.('WebSocket 连接失败,正在尝试重连...')
      // onclose 会自动触发
    }
  }

  function scheduleReconnect() {
    if (closed) return
    reconnectAttempts++
    if (reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
      closed = true
      opts.onError?.('多次重连失败,已停止自动重连;请刷新页面重试')
      opts.onStatus?.('error')
      return
    }
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
      terminateForBusiness()
    },
  }
}
