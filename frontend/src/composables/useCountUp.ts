/**
 * useCountUp · 数字滚动 composable
 * 首次异步加载默认直接显示最终值；animateFirstUpdate 可选启用首值滚动。
 * 后续变化从旧值平滑滚动到新值(cubic-out 缓动), prefers-reduced-motion 时直接落地。
 */
import { onScopeDispose, ref, watch, type Ref } from 'vue'

export function useCountUp(
  source: Ref<number>,
  duration = 650,
  animateFirstUpdate = false,
): Ref<number> {
  const display = ref(source.value)
  let rafId: number | null = null
  let hasReceivedFirstUpdate = false

  const reduceMotion = (): boolean =>
    typeof window !== 'undefined' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches

  const cancelPendingFrame = (): void => {
    if (rafId === null) return
    window.cancelAnimationFrame(rafId)
    rafId = null
  }

  watch(source, (to, from) => {
    cancelPendingFrame()
    let startValue = from
    if (!hasReceivedFirstUpdate) {
      hasReceivedFirstUpdate = true
      if (!animateFirstUpdate || reduceMotion() || !Number.isFinite(to) || to === display.value) {
        display.value = to
        return
      }
      startValue = display.value
    }
    if (reduceMotion() || !Number.isFinite(to) || !Number.isFinite(startValue)) {
      display.value = to
      return
    }
    const start = performance.now()
    const delta = to - startValue
    if (delta === 0) return

    const tick = (now: number): void => {
      const t = Math.min(1, (now - start) / duration)
      const eased = 1 - Math.pow(1 - t, 3)
      display.value = startValue + delta * eased
      if (t < 1) rafId = window.requestAnimationFrame(tick)
      else rafId = null
    }
    rafId = window.requestAnimationFrame(tick)
  })

  onScopeDispose(cancelPendingFrame)

  return display
}
