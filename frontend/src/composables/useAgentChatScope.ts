import { onBeforeUnmount, watch } from 'vue'
import type { ResponsesStreamHandle } from '@/types/responses'

/** 将迟到的请求、流事件和收尾绑定到发起时的账号/会话生命周期。 */
export function useAgentChatScope(
  account: () => string | number | null | undefined,
  token: () => string | undefined,
  session: () => string,
) {
  let accountGeneration = 0
  let sessionGeneration = 0
  let disposed = false
  const streams = new Set<ResponsesStreamHandle>()

  function detachStreams(): void {
    for (const stream of streams) stream.abort()
    streams.clear()
  }

  watch([account, token], () => {
    accountGeneration += 1
    sessionGeneration += 1
    detachStreams()
  }, { flush: 'sync' })
  watch(session, () => { sessionGeneration += 1 }, { flush: 'sync' })
  onBeforeUnmount(() => {
    disposed = true
    detachStreams()
  })

  function captureAccount(): () => boolean {
    const generation = accountGeneration
    return () => !disposed && generation === accountGeneration
  }

  function capture(): () => boolean {
    const generation = sessionGeneration
    const isAccount = captureAccount()
    return () => isAccount() && generation === sessionGeneration
  }

  function track(handle: ResponsesStreamHandle): void {
    streams.add(handle)
    void handle.done.finally(() => streams.delete(handle)).catch(() => undefined)
  }

  return { capture, captureAccount, track }
}
