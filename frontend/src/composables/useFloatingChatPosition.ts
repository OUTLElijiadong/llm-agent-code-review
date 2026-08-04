import { computed, onBeforeUnmount, ref, type CSSProperties } from 'vue'

interface Point { left: number; top: number }

const EDGE = 12

function storageName(storageKey: string): string {
  return `prism-floating-chat-position:${storageKey}`
}

function clamp(point: Point, panel: HTMLElement): Point {
  const width = panel.offsetWidth
  const height = panel.offsetHeight
  return {
    left: Math.max(EDGE, Math.min(point.left, Math.max(EDGE, window.innerWidth - width - EDGE))),
    top: Math.max(EDGE, Math.min(point.top, Math.max(EDGE, window.innerHeight - height - EDGE))),
  }
}

export function useFloatingChatPosition(storageKey: string) {
  const panelRef = ref<HTMLElement | null>(null)
  const position = ref<Point | null>(null)
  const dragging = ref(false)
  let start: Point | null = null
  let origin: Point | null = null

  const style = computed<CSSProperties>(() => position.value ? {
    position: 'fixed',
    left: `${position.value.left}px`,
    top: `${position.value.top}px`,
    right: 'auto',
    bottom: 'auto',
  } : {})

  function mobile(): boolean {
    return window.innerWidth <= 520
  }

  function restoreOrAnchor(): void {
    const panel = panelRef.value
    if (!panel || mobile()) return
    let saved: Point | null = null
    try {
      const parsed = JSON.parse(window.localStorage.getItem(storageName(storageKey)) || 'null') as Point | null
      if (parsed && Number.isFinite(parsed.left) && Number.isFinite(parsed.top)) saved = parsed
    } catch { /* ignore malformed local state */ }
    position.value = clamp(saved ?? {
      left: window.innerWidth - panel.offsetWidth - 24,
      top: window.innerHeight - panel.offsetHeight - 24,
    }, panel)
  }

  function persist(): void {
    if (!position.value) return
    try { window.localStorage.setItem(storageName(storageKey), JSON.stringify(position.value)) } catch { /* ignore */ }
  }

  function beginDrag(event: PointerEvent): void {
    if (mobile() || event.button !== 0 || !panelRef.value) return
    event.preventDefault()
    const current = position.value ?? clamp(panelRef.value.getBoundingClientRect(), panelRef.value)
    position.value = current
    start = { left: event.clientX, top: event.clientY }
    origin = current
    dragging.value = true
    ;(event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId)
  }

  function moveDrag(event: PointerEvent): void {
    if (!dragging.value || !start || !origin || !panelRef.value) return
    position.value = clamp({
      left: origin.left + event.clientX - start.left,
      top: origin.top + event.clientY - start.top,
    }, panelRef.value)
  }

  function endDrag(): void {
    if (!dragging.value) return
    dragging.value = false
    start = null
    origin = null
    persist()
  }

  function clampToViewport(): void {
    if (panelRef.value && position.value && !mobile()) position.value = clamp(position.value, panelRef.value)
  }

  window.addEventListener('resize', clampToViewport)
  onBeforeUnmount(() => window.removeEventListener('resize', clampToViewport))

  return { panelRef, style, dragging, restoreOrAnchor, beginDrag, moveDrag, endDrag }
}
