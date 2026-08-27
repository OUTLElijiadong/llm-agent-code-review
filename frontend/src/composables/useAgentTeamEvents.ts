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
  onStatus?: (status: AgentTeamStatus, teamId: number) => void
}

interface TeamTracker {
  afterId: number
  timer?: number
  generation: number
  inFlight: boolean
  stopped: boolean
  failures: number
}

const PERMANENT_ERROR_CODES = new Set([40331, 40431])

function errorCode(error: unknown): number | undefined {
  if (!error || typeof error !== 'object') return undefined
  const code = (error as { code?: unknown }).code
  return typeof code === 'number' ? code : undefined
}

function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message
  if (error && typeof error === 'object') {
    const message = (error as { message?: unknown }).message
    if (typeof message === 'string' && message) return message
  }
  return String(error)
}

export function useAgentTeamEvents(options: UseAgentTeamEventsOptions = {}) {
  const interval = options.interval ?? 1200
  const pageSize = options.pageSize ?? 200

  const events = ref<AgentTeamEvent[]>([]) as Ref<AgentTeamEvent[]>
  const teamStatus = ref<AgentTeamStatus | ''>('')
  const teamStatuses = ref<Record<number, AgentTeamStatus>>({})
  const loading = ref(false)
  const error = ref('')

  const trackers = new Map<number, TeamTracker>()
  let generationSeed = 0

  function isCurrent(teamId: number, tracker: TeamTracker): boolean {
    return trackers.get(teamId) === tracker && !tracker.stopped
  }

  function syncLoading(): void {
    loading.value = [...trackers.values()].some((tracker) => tracker.inFlight)
  }

  function schedule(teamId: number, tracker: TeamTracker): void {
    if (!isCurrent(teamId, tracker)) return
    window.clearTimeout(tracker.timer)
    const delay = Math.min(interval * (2 ** Math.min(tracker.failures, 3)), interval * 8)
    tracker.timer = window.setTimeout(() => void pull(teamId, tracker), delay)
  }

  async function pull(teamId: number, tracker: TeamTracker): Promise<void> {
    if (!isCurrent(teamId, tracker) || tracker.inFlight) return
    tracker.inFlight = true
    syncLoading()
    try {
      let hasMore = false
      let status: AgentTeamStatus | '' = ''
      do {
        const page = await listAgentTeamEvents(teamId, tracker.afterId, pageSize)
        if (!isCurrent(teamId, tracker)) return
        const batch = page.items.map((item) => ({ ...item, team_id: item.team_id ?? teamId }))
        if (batch.length) {
          events.value = events.value.concat(batch)
          options.onEvents?.(batch)
        }
        tracker.afterId = page.next_after_id
        status = page.team_status
        hasMore = page.has_more
      } while (hasMore && isCurrent(teamId, tracker))

      if (!isCurrent(teamId, tracker)) return
      tracker.failures = 0
      error.value = ''
      if (status) {
        const previous = teamStatuses.value[teamId]
        teamStatuses.value = { ...teamStatuses.value, [teamId]: status }
        teamStatus.value = status
        if (previous !== status) options.onStatus?.(status, teamId)
        if (TERMINAL_STATUS.has(status)) {
          stop(teamId)
          return
        }
      }
      schedule(teamId, tracker)
    } catch (caught) {
      if (!isCurrent(teamId, tracker)) return
      error.value = errorMessage(caught)
      tracker.failures += 1
      if (PERMANENT_ERROR_CODES.has(errorCode(caught) ?? -1)) {
        stop(teamId)
        return
      }
      schedule(teamId, tracker)
    } finally {
      tracker.inFlight = false
      syncLoading()
    }
  }

  /** 开始跟踪一个团队；多个团队拥有相互隔离的游标和请求代际。 */
  function start(id: number): void {
    if (!Number.isInteger(id) || id <= 0) return
    const existing = trackers.get(id)
    if (existing && !existing.stopped) return
    if (existing?.timer !== undefined) window.clearTimeout(existing.timer)
    const tracker: TeamTracker = {
      afterId: 0,
      generation: ++generationSeed,
      inFlight: false,
      stopped: false,
      failures: 0,
    }
    trackers.set(id, tracker)
    error.value = ''
    void pull(id, tracker)
  }

  function stop(id?: number): void {
    const ids = id === undefined ? [...trackers.keys()] : [id]
    for (const teamId of ids) {
      const tracker = trackers.get(teamId)
      if (!tracker) continue
      tracker.stopped = true
      window.clearTimeout(tracker.timer)
      tracker.timer = undefined
      trackers.delete(teamId)
    }
    syncLoading()
  }

  onBeforeUnmount(stop)

  return { events, teamStatus, teamStatuses, loading, error, start, stop }
}
