import { onBeforeUnmount, watch } from 'vue'

/** SSE 占用会话轮询时，仍从账本持续发现和同步团队；不等待工具或回答结束。 */
export function useLiveAgentTeamPolling(options: {
  active: () => boolean
  scope: () => string
  refresh: () => Promise<void>
}): void {
  let timer: number | undefined
  let generation = 0
  function stop(): void {
    generation += 1
    window.clearTimeout(timer)
    timer = undefined
  }
  watch(() => [options.active(), options.scope()] as const, ([active, scope]) => {
    stop()
    if (!active || !scope) return
    const current = generation
    const valid = () => current === generation && options.active() && options.scope() === scope
    const pull = async () => {
      if (!valid()) return
      try {
        await options.refresh()
      } catch {
        // 刷新函数保留可见错误及上次状态；暂时失败仍继续同步。
      } finally {
        if (valid()) timer = window.setTimeout(() => void pull(), 1000)
      }
    }
    timer = window.setTimeout(() => void pull(), 0)
  }, { immediate: true })
  onBeforeUnmount(stop)
}
