import { computed, onBeforeUnmount, ref, type CSSProperties } from 'vue'

interface Point { left: number; top: number }
interface Size { width: number; height: number }
interface SavedPanelState extends Point, Size {}

const EDGE = 12
const DEFAULT_SIZE: Size = { width: 400, height: 620 }
const MIN_SIZE: Size = { width: 360, height: 420 }
const MAX_SIZE: Size = { width: 760, height: 900 }
const KEYBOARD_RESIZE_STEP = 16

function storageName(storageKey: string): string {
  return `prism-floating-chat-position:${storageKey}`
}

function clampSize(size: Size): Size {
  const maxWidth = Math.max(1, Math.min(MAX_SIZE.width, window.innerWidth - EDGE * 2))
  const maxHeight = Math.max(1, Math.min(MAX_SIZE.height, window.innerHeight - EDGE * 2))
  return {
    width: Math.max(Math.min(MIN_SIZE.width, maxWidth), Math.min(size.width, maxWidth)),
    height: Math.max(Math.min(MIN_SIZE.height, maxHeight), Math.min(size.height, maxHeight)),
  }
}

function clamp(point: Point, size: Size): Point {
  return {
    left: Math.max(EDGE, Math.min(point.left, Math.max(EDGE, window.innerWidth - size.width - EDGE))),
    top: Math.max(EDGE, Math.min(point.top, Math.max(EDGE, window.innerHeight - size.height - EDGE))),
  }
}

export function useFloatingChatPosition(storageKey: string) {
  const panelRef = ref<HTMLElement | null>(null)
  const position = ref<Point | null>(null)
  const size = ref<Size>({ ...DEFAULT_SIZE })
  const dragging = ref(false)
  const resizing = ref(false)
  let start: Point | null = null
  let origin: Point | Size | null = null

  const style = computed<CSSProperties>(() => position.value && !mobile() ? {
    position: 'fixed',
    left: `${position.value.left}px`,
    top: `${position.value.top}px`,
    right: 'auto',
    bottom: 'auto',
    width: `${size.value.width}px`,
    height: `${size.value.height}px`,
  } : {})

  function mobile(): boolean {
    return window.innerWidth <= 520
  }

  function restoreOrAnchor(): void {
    const panel = panelRef.value
    if (!panel) return
    if (mobile()) {
      // 移动端由组件的响应式 inset 定位。清掉桌面拖拽坐标，避免浮窗留在视口外。
      position.value = null
      return
    }
    // Teleport + 异步组件自动恢复时，面板可能刚挂载但尚未完成布局。
    // 不能用 0×0 计算右下角坐标，否则真实尺寸出来后整个窗口会落到视口外。
    if (panel.offsetWidth <= 0 || panel.offsetHeight <= 0) {
      window.requestAnimationFrame(restoreOrAnchor)
      return
    }
    let savedPosition: Point | null = null
    let savedSize: Partial<Size> | null = null
    try {
      const parsed = JSON.parse(window.localStorage.getItem(storageName(storageKey)) || 'null') as Partial<SavedPanelState> | null
      if (parsed && Number.isFinite(parsed.left) && Number.isFinite(parsed.top)) {
        savedPosition = { left: parsed.left!, top: parsed.top! }
        savedSize = parsed
      }
    } catch { /* ignore malformed local state */ }
    const savedWidth = savedSize?.width
    const savedHeight = savedSize?.height
    size.value = clampSize({
      width: typeof savedWidth === 'number' && Number.isFinite(savedWidth)
        ? savedWidth
        : panel.offsetWidth || DEFAULT_SIZE.width,
      height: typeof savedHeight === 'number' && Number.isFinite(savedHeight)
        ? savedHeight
        : panel.offsetHeight || DEFAULT_SIZE.height,
    })
    position.value = clamp(savedPosition ?? {
      left: window.innerWidth - size.value.width - 24,
      top: window.innerHeight - size.value.height - 24,
    }, size.value)
  }

  function persist(): void {
    if (!position.value) return
    try { window.localStorage.setItem(storageName(storageKey), JSON.stringify({ ...position.value, ...size.value })) } catch { /* ignore */ }
  }

  function beginDrag(event: PointerEvent): void {
    if (mobile() || event.button !== 0 || !panelRef.value) return
    event.preventDefault()
    const current = position.value ?? clamp(panelRef.value.getBoundingClientRect(), size.value)
    position.value = current
    start = { left: event.clientX, top: event.clientY }
    origin = current
    dragging.value = true
    ;(event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId)
  }

  function moveDrag(event: PointerEvent): void {
    if (!start || !origin || !panelRef.value) return
    if (dragging.value && 'left' in origin && 'top' in origin) {
      position.value = clamp({
        left: origin.left + event.clientX - start.left,
        top: origin.top + event.clientY - start.top,
      }, size.value)
    } else if (resizing.value && 'width' in origin && 'height' in origin) {
      size.value = clampSize({
        width: origin.width + event.clientX - start.left,
        height: origin.height + event.clientY - start.top,
      })
      if (position.value) position.value = clamp(position.value, size.value)
    }
  }

  function beginResize(event: PointerEvent): void {
    if (mobile() || event.button !== 0 || !panelRef.value) return
    event.preventDefault()
    const panel = panelRef.value
    size.value = clampSize({ width: panel.offsetWidth, height: panel.offsetHeight })
    position.value = clamp(position.value ?? panel.getBoundingClientRect(), size.value)
    start = { left: event.clientX, top: event.clientY }
    origin = { ...size.value }
    resizing.value = true
    ;(event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId)
  }

  function resizeByKeyboard(event: KeyboardEvent): void {
    if (mobile()) return
    const delta = KEYBOARD_RESIZE_STEP
    const change = {
      width: event.key === 'ArrowRight' ? delta : event.key === 'ArrowLeft' ? -delta : 0,
      height: event.key === 'ArrowDown' ? delta : event.key === 'ArrowUp' ? -delta : 0,
    }
    if (!change.width && !change.height) return
    event.preventDefault()
    size.value = clampSize({ width: size.value.width + change.width, height: size.value.height + change.height })
    if (position.value) position.value = clamp(position.value, size.value)
    persist()
  }

  function endDrag(): void {
    if (!dragging.value && !resizing.value) return
    dragging.value = false
    resizing.value = false
    start = null
    origin = null
    persist()
  }

  function clampToViewport(): void {
    if (!panelRef.value) return
    if (mobile()) {
      dragging.value = false
      resizing.value = false
      start = null
      origin = null
      position.value = null
      return
    }
    size.value = clampSize(size.value)
    if (position.value) position.value = clamp(position.value, size.value)
    else restoreOrAnchor()
    persist()
  }

  window.addEventListener('resize', clampToViewport)
  onBeforeUnmount(() => window.removeEventListener('resize', clampToViewport))

  return { panelRef, style, dragging, resizing, restoreOrAnchor, beginDrag, beginResize, resizeByKeyboard, moveDrag, endDrag }
}
