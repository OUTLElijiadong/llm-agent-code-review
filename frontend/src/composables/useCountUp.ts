/**
 * useCountUp · 数字滚动 composable
 * 数字变化时从旧值平滑滚动到新值(cubic-out 缓动),
 * prefers-reduced-motion 时直接落地不滚动。
 */
import { ref, watch, type Ref } from 'vue'

export function useCountUp(
  source: Ref<number>,
  duration = 650,
): Ref<number> {
  const display = ref(source.value)
  let rafId: number | null = null

  const reduceMotion = (): boolean =>
    typeof window !== 'undefined' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches

  watch(source, (to, from) => {
    if (rafId !== null) cancelAnimationFrame(rafId)
    if (reduceMotion() || !Number.isFinite(to) || !Number.isFinite(from)) {
      display.value = to
      return
    }
    const start = performance.now()
    const delta = to - from
    if (delta === 0) return

    const tick = (now: number): void => {
      const t = Math.min(1, (now - start) / duration)
      const eased = 1 - Math.pow(1 - t, 3)
      display.value = from + delta * eased
      if (t < 1) rafId = requestAnimationFrame(tick)
    }
    rafId = requestAnimationFrame(tick)
  })

  return display
}
