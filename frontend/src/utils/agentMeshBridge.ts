import {
  AGENT_MESH_SESSION_GONE_CODE,
  heartbeatAgentMesh,
  pullAgentMeshInbox,
  type AgentMeshMessage,
  type AgentMeshSurface,
} from '@/api/agentMesh'

export interface AgentMeshBridgeOptions {
  surface: AgentMeshSurface
  getSessionId: () => string
  getTitle: () => string
  getSessions?: () => Array<{
    id: string
    title: string
    active_run_id?: string
    active_run_status?: string
  }>
  getActiveRun?: () => { run_id?: string; status?: string } | null
  isBusy: (sessionId: string) => boolean
  onMessage: (message: AgentMeshMessage, sessionId: string) => Promise<boolean>
  /** 返回 false 的消息不进入 Responses;宿主可用 onDeferredMessage 做审计回执。 */
  shouldAutoProcess?: (message: AgentMeshMessage, sessionId: string) => boolean
  onDeferredMessage?: (message: AgentMeshMessage, sessionId: string) => Promise<boolean>
  /**
   * 某会话被判定「已归档/未注册」(轮询命中 40321)时回调。
   * 宿主应让切换器从服务端重新收敛会话列表,把该会话剔除,停止后续轮询。
   */
  onSessionGone?: (sessionId: string) => void
  intervalMs?: number
}

export interface AgentMeshBridge {
  start: () => void
  stop: () => void
  syncNow: () => Promise<void>
  /** 仅在服务端显式恢复成功后解除该会话的归档跳过标记。 */
  reviveSession: (sessionId: string) => void
}

/** 从 axios reject 的对象里读出后端业务码(Resp 或 AxiosError 两种形态都兼容)。 */
function errorCode(reason: unknown): number | undefined {
  if (!reason || typeof reason !== 'object') return undefined
  // http.ts 拦截器 reject 的是 data(Resp) —— 直接带 code
  const direct = (reason as { code?: unknown }).code
  if (typeof direct === 'number') return direct
  return undefined
}

/** heartbeat 的 40921 还用于其他冲突，只有明确归档时才移出轮询目录。 */
function isGoneHeartbeat(reason: unknown): boolean {
  const code = errorCode(reason)
  if (code === AGENT_MESH_SESSION_GONE_CODE) return true
  if (code !== 40921 || !reason || typeof reason !== 'object') return false
  const message = (reason as { message?: unknown }).message
  return typeof message === 'string' && message.includes('会话已归档')
}

/** Axios 没有收到 HTTP 响应时的网络/超时错误；与服务端业务拒绝分开处理。 */
function isTransportFailure(reason: unknown): boolean {
  if (!reason || typeof reason !== 'object') return false
  const error = reason as { code?: unknown; response?: unknown }
  return !error.response && (
    error.code === 'ERR_NETWORK' || error.code === 'ECONNABORTED' || error.code === 'ETIMEDOUT'
  )
}

export function createAgentMeshBridge(options: AgentMeshBridgeOptions): AgentMeshBridge {
  const handled = new Set<string>()
  /** 本轮已判定归档/未注册的会话:跳过轮询,避免对死会话反复 heartbeat+403。 */
  const goneSessions = new Set<string>()
  let timer: number | undefined
  let started = false
  let transportFailures = 0
  let stopped = false
  let generation = 0
  let syncingGeneration: number | undefined

  function markGone(sessionId: string): void {
    if (goneSessions.has(sessionId)) return
    goneSessions.add(sessionId)
    options.onSessionGone?.(sessionId)
  }

  function reviveSession(sessionId: string): void {
    if (sessionId) goneSessions.delete(sessionId)
  }

  async function syncNow(): Promise<void> {
    if (stopped || syncingGeneration === generation) return
    const syncGeneration = generation
    const isCurrentSync = () => !stopped && generation === syncGeneration
    const currentSessionId = options.getSessionId()
    const configuredSessions = options.getSessions?.() ?? []
    const sessions = configuredSessions.length
      ? configuredSessions
      : currentSessionId
        ? [{ id: currentSessionId, title: options.getTitle() }]
        : []
    if (!sessions.length) return
    syncingGeneration = syncGeneration
    let transportFailed = false
    try {
      const activeRun = options.getActiveRun?.()
      const failedHeartbeatSessions = new Set<string>()
      for (const session of sessions) {
        if (!isCurrentSync()) return
        // 已归档会话跳过 heartbeat:服务端本就不会复活它,徒增一次无效请求。
        if (goneSessions.has(session.id)) continue
        const isCurrent = session.id === currentSessionId
        try {
          await heartbeatAgentMesh({
            surface: options.surface,
            session_id: session.id,
            title: session.title.trim() || '新对话',
            active_run_id: session.active_run_id ?? (isCurrent ? activeRun?.run_id : '') ?? '',
            active_run_status: session.active_run_status ?? (isCurrent ? activeRun?.status : '') ?? '',
          }, true)
          if (!isCurrentSync()) return
        } catch (reason) {
          if (!isCurrentSync()) return
          // 断网时继续遍历会话只会制造一批相同失败；整轮停止并退避。
          if (isTransportFailure(reason)) {
            transportFailed = true
            transportFailures = Math.min(transportFailures + 1, 3)
            return
          }
          if (isGoneHeartbeat(reason)) markGone(session.id)
          else failedHeartbeatSessions.add(session.id)
          // 单个后台会话的业务失败不阻断其余会话的消息回收。
          continue
        }
      }
      // 优先认领当前会话的收件箱,避免历史会话的主动简报占满串行处理队列,
      // 导致用户正在看的对话迟迟收不到 JARVIS 简报/团队结论等消息。
      const orderedSessions = [...sessions].sort(
        (left, right) => Number(right.id === currentSessionId) - Number(left.id === currentSessionId),
      )
      for (const session of orderedSessions) {
        if (!isCurrentSync()) return
        if (goneSessions.has(session.id)) continue
        if (failedHeartbeatSessions.has(session.id)) continue
        if (options.isBusy(session.id)) continue
        let inbox: AgentMeshMessage[]
        try {
          inbox = await pullAgentMeshInbox(options.surface, session.id, 20)
          if (!isCurrentSync()) return
        } catch (reason) {
          if (!isCurrentSync()) return
          if (isTransportFailure(reason)) {
            transportFailed = true
            transportFailures = Math.min(transportFailures + 1, 3)
            return
          }
          // 会话已归档/未注册:正常生命周期,标记后跳过,并通知宿主收敛会话列表。
          if (errorCode(reason) === AGENT_MESH_SESSION_GONE_CODE) {
            markGone(session.id)
            continue
          }
          throw reason
        }
        const message = inbox.find((item) => (
          item.status === 'delivered' && !handled.has(item.message_id)
        ))
        if (!message) continue
        if (options.shouldAutoProcess && !options.shouldAutoProcess(message, session.id)) {
          const deferred = options.onDeferredMessage
            ? await options.onDeferredMessage(message, session.id)
            : false
          if (!isCurrentSync()) return
          if (deferred) handled.add(message.message_id)
          return
        }
        const received = await options.onMessage(message, session.id)
        if (!isCurrentSync()) return
        if (received) handled.add(message.message_id)
        return
      }
    } catch {
      // 短暂网络故障由下一轮心跳重试，不干扰用户当前对话。
    } finally {
      if (isCurrentSync() && !transportFailed) transportFailures = 0
      if (syncingGeneration === syncGeneration) syncingGeneration = undefined
    }
  }

  function scheduleNext(): void {
    if (!started || stopped || timer !== undefined) return
    const delay = (options.intervalMs ?? 5_000) * (2 ** transportFailures)
    timer = window.setTimeout(() => {
      timer = undefined
      void syncNow().then(scheduleNext, scheduleNext)
    }, delay)
  }

  function start(): void {
    if (started) return
    started = true
    stopped = false
    void syncNow().then(scheduleNext, scheduleNext)
  }

  function stop(): void {
    started = false
    stopped = true
    generation += 1
    syncingGeneration = undefined
    if (timer !== undefined) window.clearTimeout(timer)
    timer = undefined
    transportFailures = 0
    // 宿主在切换登录账号时复用 bridge 实例，绝不沿用旧账号的去重/失效标记。
    handled.clear()
    goneSessions.clear()
  }

  return { start, stop, syncNow, reviveSession }
}
