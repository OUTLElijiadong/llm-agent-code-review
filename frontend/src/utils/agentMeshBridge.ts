import {
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
  intervalMs?: number
}

export interface AgentMeshBridge {
  start: () => void
  stop: () => void
  syncNow: () => Promise<void>
}

export function createAgentMeshBridge(options: AgentMeshBridgeOptions): AgentMeshBridge {
  const handled = new Set<string>()
  let timer: number | undefined
  let syncing = false

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
        const isCurrent = session.id === currentSessionId
        await heartbeatAgentMesh({
          surface: options.surface,
          session_id: session.id,
          title: session.title.trim() || '新对话',
          active_run_id: session.active_run_id ?? (isCurrent ? activeRun?.run_id : '') ?? '',
          active_run_status: session.active_run_status ?? (isCurrent ? activeRun?.status : '') ?? '',
        })
      }
      for (const session of sessions) {
        if (options.isBusy(session.id)) continue
        const inbox = await pullAgentMeshInbox(options.surface, session.id, 20)
        const message = inbox.find((item) => (
          item.status === 'delivered' && !handled.has(item.message_id)
        ))
        if (!message) continue
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
