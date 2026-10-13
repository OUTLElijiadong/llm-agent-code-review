import { onBeforeUnmount, onMounted, watch } from 'vue'
import { AGENT_PAGE_INVALIDATED_EVENT } from '@/utils/agentPageActions'
import { getToken } from '@/utils/token'

/** 仅刷新页面的业务读取，不重挂组件；编辑中的页面延后到草稿操作结束。 */
export function useAgentPageRefresh(options: {
  pages: string[]
  refresh: () => unknown | Promise<unknown>
  canRefresh?: () => boolean
}): void {
  let pending = false
  let disposed = false
  let accountToken: string | null = null
  let timer: ReturnType<typeof setTimeout> | undefined
  function schedule(): void {
    if (!pending || disposed || timer !== undefined || options.canRefresh?.() === false) return
    timer = setTimeout(() => {
      timer = undefined
      if (disposed || options.canRefresh?.() === false) return
      if (accountToken !== getToken()) { pending = false; return }
      pending = false
      void Promise.resolve().then(options.refresh).catch(() => undefined)
    }, 30)
  }
  function handle(event: Event): void {
    const detail = (event as CustomEvent<{ page?: unknown }>).detail
    if (!detail || typeof detail.page !== 'string') return
    const pathname = detail.page.split(/[?#]/, 1)[0]
    if (!options.pages.some(page => pathname === page || pathname.startsWith(`${page}/`))) return
    pending = true
    accountToken = getToken()
    schedule()
  }
  if (options.canRefresh) watch(options.canRefresh, schedule)
  onMounted(() => window.addEventListener(AGENT_PAGE_INVALIDATED_EVENT, handle))
  onBeforeUnmount(() => {
    disposed = true
    if (timer !== undefined) clearTimeout(timer)
    window.removeEventListener(AGENT_PAGE_INVALIDATED_EVENT, handle)
  })
}
