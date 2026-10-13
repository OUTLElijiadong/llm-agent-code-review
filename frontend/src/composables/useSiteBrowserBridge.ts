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
    if (disposed || recovering || !user.token || !options.runId()) return
    recovering = true
    try { await bridge.recover() } finally { recovering = false }
    if (!disposed && ['running', 'queued', 'waiting_browser'].includes(options.status())) {
      stop(); timer = setTimeout(() => { timer = undefined; void recover() }, 2000)
    }
  }
  watch([() => user.profile?.id, () => user.token, options.sessionId, options.runId], () => {
    stop(); bridge.reset()
    if (options.runId() && ['running', 'queued', 'waiting_browser'].includes(options.status())) void recover()
  }, { flush: 'sync' })
  watch(options.status, status => {
    if (['running', 'queued', 'waiting_browser'].includes(status)) void recover()
    else { stop(); if (['completed', 'cancelled', 'failed', 'incomplete', 'max_rounds_exceeded'].includes(status)) bridge.reset() }
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
