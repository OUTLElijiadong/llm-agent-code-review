/**
 * 待审批的远程授权申请数量（侧栏角标）。
 *
 * 只给拥有 `pentest:authorize` 的账号统计，且不计入自己提交的申请（自己不能批准自己）。
 * 角标只是提示：接口失败静默保留旧值；页面在后台时跳过轮询；申请、批准或取消动作发出
 * 变更事件后立即刷新。
 */
import { onScopeDispose, ref, watch } from 'vue'

import { getPendingRemoteApprovalSnapshot } from '@/api/remotePentest'
import { REMOTE_PENTEST_CHANGED_EVENT } from '@/utils/remotePentestEvents'

export { REMOTE_PENTEST_CHANGED_EVENT }

/** 轮询间隔：审批申请不是实时场景，一分钟足够，也避免侧栏给后端增加明显压力。 */
export const PENDING_REMOTE_APPROVALS_POLL_MS = 60_000

export function usePendingRemoteApprovals(options: {
  /** 当前账号是否有资格批准申请。 */
  enabled: () => boolean
  /** 当前账号 ID，用于排除自己提交的申请。 */
  currentUserId: () => number | undefined
}) {
  const count = ref(0)
  let timer: ReturnType<typeof setInterval> | undefined
  let disposed = false
  let generation = 0

  async function refresh(): Promise<void> {
    if (disposed) return
    if (!options.enabled()) {
      count.value = 0
      return
    }
    const ticket = ++generation
    try {
      const snapshot = await getPendingRemoteApprovalSnapshot()
      // 期间权限被撤销、账号切换或又发起了更新的请求时，丢弃这次迟到的结果。
      if (disposed || ticket !== generation || !options.enabled()) return
      const me = options.currentUserId()
      count.value = snapshot.items.filter((item) => item.owner_id !== me).length
    } catch {
      /* 角标只是提示，失败时保留旧计数，不打扰用户 */
    }
  }

  function poll(): void {
    if (typeof document !== 'undefined' && document.hidden) return
    void refresh()
  }

  function start(): void {
    stop()
    timer = setInterval(poll, PENDING_REMOTE_APPROVALS_POLL_MS)
  }

  function stop(): void {
    if (timer !== undefined) clearInterval(timer)
    timer = undefined
  }

  function onChanged(): void {
    void refresh()
  }

  // 组件 setup 与独立 effectScope 都会在销毁时触发 onScopeDispose，因此无需依赖挂载钩子。
  window.addEventListener(REMOTE_PENTEST_CHANGED_EVENT, onChanged)
  void refresh()
  start()

  onScopeDispose(() => {
    disposed = true
    stop()
    window.removeEventListener(REMOTE_PENTEST_CHANGED_EVENT, onChanged)
  })

  watch(
    [() => options.enabled(), () => options.currentUserId()],
    () => {
      count.value = 0
      void refresh()
    },
  )

  return { count, refresh }
}
