/**
 * 轮询 agent team 增量事件流(思考城市多Agent联动用)
 * ------------------------------------------------------------------
 * 后端只提供 HTTP 增量端点(GET /agent-teams/{id}/events?after_id=N),
 * 这里封装成「响应式事件流」:首次全量,之后每 interval 拿新事件,
 * team_status 到终态后自动停止。
 */
import { onBeforeUnmount, ref, type Ref } from 'vue'

import { listAgentTeamEvents, type AgentTeamEvent, type AgentTeamStatus } from '@/api/agentTeams'

const TERMINAL_STATUS: ReadonlySet<AgentTeamStatus> = new Set([
  'completed',
  'failed',
  'cancelled',
  'expired',
])

export interface UseAgentTeamEventsOptions {
  /** 轮询间隔(ms),默认 1200 */
  interval?: number
  /** 每页拉取条数,默认 200 */
  pageSize?: number
  /** 每批新事件回调(可用于驱动可视化) */
  onEvents?: (events: AgentTeamEvent[]) => void
  /** 团队状态变化回调 */
  onStatus?: (status: AgentTeamStatus) => void
}

export function useAgentTeamEvents(options: UseAgentTeamEventsOptions = {}) {
  const interval = options.interval ?? 1200
  const pageSize = options.pageSize ?? 200

  const events = ref<AgentTeamEvent[]>([]) as Ref<AgentTeamEvent[]>
  const teamStatus = ref<AgentTeamStatus | ''>('')
  const loading = ref(false)
  const error = ref('')

  let teamId: number | null = null
  let afterId = 0
  let timer: number | undefined
  let stopped = true
  /** 防止上一次请求还没回就发下一次 */
  let inFlight = false

  async function pull(): Promise<void> {
    if (teamId === null || inFlight) return
    inFlight = true
    try {
      const page = await listAgentTeamEvents(teamId, afterId, pageSize)
      if (page.items.length) {
        events.value = events.value.concat(page.items)
        afterId = page.next_after_id
        options.onEvents?.(page.items)
      } else {
        afterId = page.next_after_id
      }
      if (page.team_status !== teamStatus.value) {
        teamStatus.value = page.team_status
        options.onStatus?.(page.team_status)
      }
      error.value = ''
      // 终态后停止轮询
      if (TERMINAL_STATUS.has(page.team_status)) stop()
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
      // 出错不退避太狠,下一轮照常(团队可能还没建完)
    } finally {
      inFlight = false
    }
  }

  function schedule(): void {
    if (stopped) return
    timer = window.setTimeout(async () => {
      await pull()
      schedule()
    }, interval)
  }

  /** 开始跟踪一个团队;重复调用会先停掉旧的 */
  function start(id: number): void {
    stop()
    teamId = id
    afterId = 0
    events.value = []
    teamStatus.value = ''
    error.value = ''
    stopped = false
    void pull()
    schedule()
  }

  function stop(): void {
    stopped = true
    window.clearTimeout(timer)
    timer = undefined
  }

  onBeforeUnmount(stop)

  return { events, teamStatus, loading, error, start, stop }
}
