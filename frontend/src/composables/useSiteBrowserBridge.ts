import { getAgentResponseSession } from '@/api/agentResponses'
import { nextTick, onBeforeUnmount, watch } from 'vue'
import type { Router } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { useAgentActivityStore } from '@/stores/agentActivity'
import { isNavigationPathAllowed } from '@/utils/agentNavigation'
import { createSiteBrowserBridge } from '@/utils/siteBrowserBridge'
import type { ResponseStreamEvent } from '@/types/responses'

/** SSE 与重连恢复共用一次执行器；同一个账号的其他标签页由服务端 claim 排他保护。 */
export function useSiteBrowserBridge(options: {
  surface: 'user' | 'admin'
  sessionId: () => string
  runId: () => string
  status: () => string
  router: Router
}) {
  const user = useUserStore(), activity = useAgentActivityStore()
  let disposed = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let recovering = false
  let generation = 0
  let confirmedRunId = ''
  const canRecover = () => ['running', 'queued', 'waiting_browser'].includes(options.status())
  const bridge = createSiteBrowserBridge({
    current: () => ({ accountId: user.profile?.id, token: user.token, sessionId: options.sessionId(),
      runId: options.runId(), surface: options.surface, route: options.router.currentRoute.value.fullPath }),
    canNavigate: path => !['/login', '/register', '/403', '/404'].includes(path.split(/[?#]/)[0]!)
      && isNavigationPathAllowed(options.router, path, user),
    navigate: route => options.router.push(route),
    isActive: () => ['running', 'approving', 'answering', 'rejecting', 'queued', 'waiting_browser'].includes(options.status()),
    settle: async () => { await nextTick(); await new Promise(resolve => setTimeout(resolve, 80)); await nextTick() },
    onStatus: status => {
      const id = `browser-${status.actionId}`
      if (status.phase === 'executing') activity.begin(status.message, id)
      else activity.end(id)
    },
  })
  function stop(): void { if (timer !== undefined) clearTimeout(timer); timer = undefined }
  async function recover(): Promise<void> {
    if (disposed || recovering || !user.token || !options.runId() || !canRecover()) return
    const epoch = generation
    const runId = options.runId(), sessionId = options.sessionId()
    const owned = () => !disposed && epoch === generation
      && runId === options.runId() && sessionId === options.sessionId()
    const current = () => owned() && canRecover()
    recovering = true
    try {
      // 本地预先生成的 ID 和 response.created 均早于运行提交，不能作为恢复依据。
      // 复用账号隔离的会话回读，未落库时等待；只对已确认的同一运行查询页面动作。
      if (confirmedRunId !== runId) {
        const session = await getAgentResponseSession(options.surface, sessionId, true)
        if (!current()) return
        if (session.surface === options.surface && session.session_id === sessionId
          && session.run?.run_id === runId
          && ['running', 'approving', 'answering'].includes(session.run.status)) confirmedRunId = runId
      }
      if (current() && confirmedRunId === runId) await bridge.recover()
    } catch {
      // 真实鉴权错误由 HTTP 层报告；网络失败不推断运行存在或执行动作。
    } finally {
      if (owned()) {
        recovering = false
        if (canRecover()) {
          stop(); timer = setTimeout(() => { timer = undefined; void recover() }, 2000)
        }
      }
    }
  }
  watch([() => user.profile?.id, () => user.token, options.sessionId, options.runId], () => {
    generation += 1; recovering = false; confirmedRunId = ''
    stop(); bridge.reset()
    if (options.runId() && canRecover()) void recover()
  }, { flush: 'sync' })
  watch(options.status, status => {
    if (canRecover()) void recover()
    else {
      stop()
      // 终态使尚未返回的会话确认失效；旧请求不能在结束后再恢复页面动作。
      if (['completed', 'cancelled', 'failed', 'incomplete', 'blocked', 'max_rounds_exceeded'].includes(status)) {
        generation += 1; recovering = false; confirmedRunId = ''; bridge.reset()
      }
    }
  })
  onBeforeUnmount(() => { disposed = true; stop(); bridge.reset() })
  return {
    handle(event: ResponseStreamEvent): boolean {
      if (event.type !== 'response.browser.required') return false
      void bridge.handle(event)
      return true
    },
    recover,
  }
}
