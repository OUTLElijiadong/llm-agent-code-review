import { computed, onBeforeUnmount, ref, watch, type CSSProperties } from 'vue'

interface Point { left: number; top: number }
interface Size { width: number; height: number }
interface PanelState extends Point, Size {}
export type FloatingChatResizeDirection = 'n' | 'ne' | 'e' | 'se' | 's' | 'sw' | 'w' | 'nw'

const EDGE = 12
const MIN_SIZE: Size = { width: 360, height: 420 }
const MAX_SIZE: Size = { width: 760, height: 900 }

function storageName(storageKey: string): string {
  return `prism-floating-chat-position:${storageKey}`
}

function responsive(): boolean {
  // 与两端组件的手机、低高度媒体查询保持一致，避免 CSS inset 与内联坐标竞争。
  return window.innerWidth <= 520 || window.innerHeight <= 520
}

function bound(value: number, min: number, max: number): number {
  return Math.max(Math.min(min, max), Math.min(value, max))
}

function clampSize(size: Size): Size {
  return {
    width: bound(size.width, MIN_SIZE.width, Math.min(MAX_SIZE.width, window.innerWidth - EDGE * 2)),
    height: bound(size.height, MIN_SIZE.height, Math.min(MAX_SIZE.height, window.innerHeight - EDGE * 2)),
  }
}

function clampPosition(point: Point, size: Size): Point {
  return {
    left: bound(point.left, EDGE, Math.max(EDGE, window.innerWidth - size.width - EDGE)),
    top: bound(point.top, EDGE, Math.max(EDGE, window.innerHeight - size.height - EDGE)),
  }
}

export function useFloatingChatPosition(storageKey: string) {
  const panelRef = ref<HTMLElement | null>(null)
  const state = ref<PanelState | null>(null)
  const dragging = ref(false)
  const resizing = ref(false)
  let preferred: PanelState | null = null
  let start: Point | null = null
  let origin: PanelState | null = null
  let direction: FloatingChatResizeDirection = 'se'
  let pointerId: number | null = null
  let captureTarget: HTMLElement | null = null
  let restoreFrame: number | null = null

  const style = computed<CSSProperties>(() => state.value ? {
    position: 'fixed', left: `${state.value.left}px`, top: `${state.value.top}px`,
    right: 'auto', bottom: 'auto', width: `${state.value.width}px`, height: `${state.value.height}px`,
  } : {})

  function cancelRestore(): void {
    if (restoreFrame !== null) window.cancelAnimationFrame(restoreFrame)
    restoreFrame = null
  }

  function restoreOrAnchor(): void {
    const panel = panelRef.value
    if (!panel) return
    if (responsive()) {
      cancelRestore()
      state.value = null
      return
    }
    // Teleport 首次布局尚未完成，不能用 0×0 计算右下角坐标。
    if (panel.offsetWidth <= 0 || panel.offsetHeight <= 0) {
      if (restoreFrame === null) restoreFrame = window.requestAnimationFrame(() => {
        restoreFrame = null
        restoreOrAnchor()
      })
      return
    }
    cancelRestore()
    let saved: Partial<PanelState> | null = null
    try {
      const parsed = JSON.parse(window.localStorage.getItem(storageName(storageKey)) || 'null')
      if (parsed && Number.isFinite(parsed.left) && Number.isFinite(parsed.top)) saved = parsed
    } catch { /* 本地偏好损坏不影响打开聊天 */ }
    const size = clampSize({
      width: typeof saved?.width === 'number' && Number.isFinite(saved.width) ? saved.width : panel.offsetWidth,
      height: typeof saved?.height === 'number' && Number.isFinite(saved.height) ? saved.height : panel.offsetHeight,
    })
    const point = clampPosition(saved ? { left: saved.left!, top: saved.top! } : {
      left: window.innerWidth - size.width - 24,
      top: window.innerHeight - size.height - 24,
    }, size)
    state.value = { ...point, ...size }
    preferred = { ...state.value }
  }

  function persist(): void {
    if (!state.value) return
    preferred = { ...state.value }
    try { window.localStorage.setItem(storageName(storageKey), JSON.stringify(state.value)) } catch { /* 存储不可用仍可操作 */ }
  }

  function startPointer(event: PointerEvent): boolean {
    if (responsive() || event.button !== 0 || !panelRef.value || start) return false
    if (!state.value) restoreOrAnchor()
    if (!state.value) return false
    event.preventDefault()
    start = { left: event.clientX, top: event.clientY }
    origin = { ...state.value }
    pointerId = event.pointerId
    captureTarget = event.currentTarget as HTMLElement
    try { captureTarget?.setPointerCapture?.(event.pointerId) } catch { /* 已结束的指针不应阻断界面 */ }
    return true
  }

  function beginDrag(event: PointerEvent): void {
    if (startPointer(event)) dragging.value = true
  }

  function beginResize(event: PointerEvent, edge: FloatingChatResizeDirection = 'se'): void {
    if (!startPointer(event)) return
    direction = edge
    resizing.value = true
  }

  function moveDrag(event: PointerEvent): void {
    if (!start || !origin || !panelRef.value || (event.pointerId !== undefined && event.pointerId !== pointerId)) return
    const dx = event.clientX - start.left
    const dy = event.clientY - start.top
    if (dragging.value) {
      state.value = { ...origin, ...clampPosition({ left: origin.left + dx, top: origin.top + dy }, origin) }
      return
    }
    const next = { ...origin }
    // 固定未拖动的对边，边角碰到视口后停止，不能把整个窗口挤走。
    if (direction.includes('e')) next.width = bound(origin.width + dx, MIN_SIZE.width, Math.min(MAX_SIZE.width, window.innerWidth - origin.left - EDGE))
    if (direction.includes('s')) next.height = bound(origin.height + dy, MIN_SIZE.height, Math.min(MAX_SIZE.height, window.innerHeight - origin.top - EDGE))
    if (direction.includes('w')) {
      next.width = bound(origin.width - dx, MIN_SIZE.width, Math.min(MAX_SIZE.width, origin.left + origin.width - EDGE))
      next.left = origin.left + origin.width - next.width
    }
    if (direction.includes('n')) {
      next.height = bound(origin.height - dy, MIN_SIZE.height, Math.min(MAX_SIZE.height, origin.top + origin.height - EDGE))
      next.top = origin.top + origin.height - next.height
    }
    state.value = next
  }

  function resizeByKeyboard(event: KeyboardEvent): void {
    if (responsive() || !panelRef.value) return
    const step = event.shiftKey ? 64 : 16
    const dx = event.key === 'ArrowRight' ? step : event.key === 'ArrowLeft' ? -step : 0
    const dy = event.key === 'ArrowDown' ? step : event.key === 'ArrowUp' ? -step : 0
    if (!dx && !dy) return
    if (!state.value) restoreOrAnchor()
    if (!state.value) return
    event.preventDefault()
    const size = clampSize({ width: state.value.width + dx, height: state.value.height + dy })
    state.value = { ...clampPosition(state.value, size), ...size }
    persist()
  }

  function releasePointer(): void {
    const target = captureTarget
    const id = pointerId
    dragging.value = resizing.value = false
    start = origin = null
    captureTarget = null
    pointerId = null
    try { if (id !== null && target?.hasPointerCapture?.(id)) target.releasePointerCapture(id) } catch { /* 节点可能已卸载 */ }
  }

  function endDrag(event?: PointerEvent): void {
    if (!start || (event?.pointerId !== undefined && event.pointerId !== pointerId)) return
    releasePointer()
    persist()
  }

  function clampToViewport(): void {
    endDrag()
    if (!panelRef.value) return
    if (responsive()) {
      cancelRestore()
      state.value = null
      return
    }
    if (!state.value) restoreOrAnchor()
    const base = preferred ?? state.value
    if (base) {
      const size = clampSize(base)
      state.value = { ...clampPosition(base, size), ...size }
    }
  }

  watch(panelRef, panel => {
    if (!panel) { endDrag(); cancelRestore() }
  })
  const handleBlur = (): void => endDrag()
  window.addEventListener('resize', clampToViewport)
  window.addEventListener('blur', handleBlur)
  onBeforeUnmount(() => {
    endDrag()
    cancelRestore()
    window.removeEventListener('resize', clampToViewport)
    window.removeEventListener('blur', handleBlur)
  })

  return { panelRef, style, dragging, resizing, restoreOrAnchor, beginDrag, beginResize, resizeByKeyboard, moveDrag, endDrag }
}
