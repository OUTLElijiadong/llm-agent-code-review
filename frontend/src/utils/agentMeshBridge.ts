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
}

/** 从 axios reject 的对象里读出后端业务码(Resp 或 AxiosError 两种形态都兼容)。 */
function errorCode(reason: unknown): number | undefined {
  if (!reason || typeof reason !== 'object') return undefined
  // http.ts 拦截器 reject 的是 data(Resp) —— 直接带 code
  const direct = (reason as { code?: unknown }).code
  if (typeof direct === 'number') return direct
  return undefined
}

export function createAgentMeshBridge(options: AgentMeshBridgeOptions): AgentMeshBridge {
  const handled = new Set<string>()
  /** 本轮已判定归档/未注册的会话:跳过轮询,避免对死会话反复 heartbeat+403。 */
  const goneSessions = new Set<string>()
  let timer: number | undefined
  let syncing = false

  function markGone(sessionId: string): void {
    if (goneSessions.has(sessionId)) return
    goneSessions.add(sessionId)
    options.onSessionGone?.(sessionId)
  }

  async function syncNow(): Promise<void> {
    if (syncing) return
    const currentSessionId = options.getSessionId()
    const configuredSessions = options.getSessions?.() ?? []
    const sessions = configuredSessions.length
      ? configuredSessions
      : currentSessionId
        ? [{ id: currentSessionId, title: options.getTitle() }]
        : []
    if (!sessions.length) return
    syncing = true
    try {
      const activeRun = options.getActiveRun?.()
      for (const session of sessions) {
        // 已归档会话跳过 heartbeat:服务端本就不会复活它,徒增一次无效请求。
        if (goneSessions.has(session.id)) continue
        const isCurrent = session.id === currentSessionId
        await heartbeatAgentMesh({
          surface: options.surface,
          session_id: session.id,
          title: session.title.trim() || '新对话',
          active_run_id: session.active_run_id ?? (isCurrent ? activeRun?.run_id : '') ?? '',
          active_run_status: session.active_run_status ?? (isCurrent ? activeRun?.status : '') ?? '',
        })
      }
      // 优先认领当前会话的收件箱,避免历史会话的主动简报占满串行处理队列,
      // 导致用户正在看的对话迟迟收不到 JARVIS 简报/团队结论等消息。
      const orderedSessions = [...sessions].sort(
        (left, right) => Number(right.id === currentSessionId) - Number(left.id === currentSessionId),
      )
      for (const session of orderedSessions) {
        if (goneSessions.has(session.id)) continue
        if (options.isBusy(session.id)) continue
        let inbox: AgentMeshMessage[]
        try {
          inbox = await pullAgentMeshInbox(options.surface, session.id, 20)
        } catch (reason) {
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
          if (deferred) handled.add(message.message_id)
          return
        }
        if (await options.onMessage(message, session.id)) handled.add(message.message_id)
        return
      }
    } catch {
      // 短暂网络故障由下一轮心跳重试，不干扰用户当前对话。
    } finally {
      syncing = false
    }
  }

  function start(): void {
    if (timer !== undefined) return
    void syncNow()
    timer = window.setInterval(() => void syncNow(), options.intervalMs ?? 5_000)
  }

  function stop(): void {
    if (timer !== undefined) window.clearInterval(timer)
    timer = undefined
  }

  return { start, stop, syncNow }
}
