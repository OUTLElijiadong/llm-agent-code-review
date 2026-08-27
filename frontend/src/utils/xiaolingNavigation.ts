/** 小菱可视化站内导航事件。 */
export const XIAOLING_NAVIGATION_EVENT = 'prism:xiaoling-navigate'

export interface XiaolingNavigationRequest {
  route: string
  label: string
  execute: () => void
  sourceElement?: HTMLElement | null
  handled: boolean
}

/** 校验小菱导航目标是否为安全的站内绝对路径。 */
export function isSafeXiaolingRoute(route: string): boolean {
  if (!route.startsWith('/') || route.startsWith('//')) return false
  try {
    const parsed = new URL(route, window.location.origin)
    return parsed.origin === window.location.origin
  } catch {
    return false
  }
}

/**
 * 请求全局虚拟鼠标展示点击过程后再执行站内导航。
 * VirtualCursor 未挂载时立即执行回调，避免导航按钮失效。
 */
export function requestXiaolingNavigation(
  route: string,
  label: string,
  execute: () => void,
  sourceElement?: HTMLElement | null,
): boolean {
  if (!isSafeXiaolingRoute(route)) return false
  const detail: XiaolingNavigationRequest = {
    route,
    label: label.trim() || '目标页面',
    execute,
    sourceElement,
    handled: false,
  }
  window.dispatchEvent(new CustomEvent<XiaolingNavigationRequest>(XIAOLING_NAVIGATION_EVENT, { detail }))
  if (!detail.handled) execute()
  return true
}
