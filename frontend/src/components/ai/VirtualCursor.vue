<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { useAgentActivityStore } from '@/stores/agentActivity'
import {
  XIAOLING_NAVIGATION_EVENT,
  isSafeXiaolingRoute,
  type XiaolingNavigationRequest,
} from '@/utils/xiaolingNavigation'

/**
 * 小菱「帮我操作」虚拟鼠标(真实定位版):
 * 后端直调业务 API 的同时,虚拟光标会:
 *  1. 按 targetHint(页面路由/区域语义)在当前页面上找到真实的目标元素
 *     (侧边导航项 / 页面内按钮),找不到时退回主内容区中心;
 *  2. 从右下滑入该元素的屏幕坐标,到达后播放点击涟漪并给目标元素
 *     加品牌色聚焦光晕——用户能看到「小菱点的是这里」;
 *  3. 目标是其他页面的导航项时,涟漪后真实触发点击完成路由跳转,
 *     实现「虚拟鼠标点到对应位置并跳转界面」的实况感。
 * 光标层 pointer-events:none,绝不拦截用户真实交互。
 */
const store = useAgentActivityStore()

const visible = ref(false)
const x = ref(0)
const y = ref(0)
/** 到达目标后为 true,触发外圈涟漪高亮 */
const arrived = ref(false)
/** 到达目标并触发真实点击时为 true,播放按下反馈。 */
const clicking = ref(false)
/** 站内导航的最终结果,只在路由实际到达后显示成功。 */
const navigationResult = ref<'success' | 'failure' | null>(null)
/** 目标元素屏幕矩形,用于定位涟漪与聚焦光晕 */
const targetBox = ref<{ left: number; top: number; width: number; height: number } | null>(null)
const cursorLayerZ = ref<string | number>('var(--layer-agent-cursor)')
const cursorStyle = computed(() => ({ transform: `translate3d(${x.value}px, ${y.value}px, 0)` }))
const haloStyle = computed(() => {
  const box = targetBox.value
  if (!box) return { display: 'none' }
  return {
    left: `${box.left}px`,
    top: `${box.top}px`,
    width: `${box.width}px`,
    height: `${box.height}px`,
  }
})

let arriveTimer: number | undefined
let clickTimer: number | undefined
let haloTimer: number | undefined
let clickStateTimer: number | undefined
let startRaf = 0
let targetRaf = 0
let activitySequence = 0
/** 已在本次活动中触发过导航,避免重复跳转 */
let navigated = false
/** 上一个被加高亮类的目标元素,结束时移除 */
let lastTargetEl: HTMLElement | null = null
/** 导航确认按钮发出的单次可视化导航请求。 */
let requestedNavigation: XiaolingNavigationRequest | null = null
let requestedActivityKey = ''
let browserTarget: HTMLElement | null = null
let browserActivityKey = ''

interface Candidate {
  el: HTMLElement
  route?: string
  activate?: () => unknown | Promise<unknown>
}

/** 装饰鼠标只跟随当前真正可命中的控件，不穿过遮罩指向后台页面。 */
function isUnobscuredTarget(el: HTMLElement): boolean {
  if (typeof document.elementFromPoint !== 'function') return true
  const rect = el.getBoundingClientRect()
  const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
  return Boolean(hit && (hit === el || el.contains(hit)))
}

function layerForTarget(el: HTMLElement): number | string {
  let top = Number(getComputedStyle(document.documentElement).getPropertyValue('--layer-agent-cursor')) || 2700
  for (let parent: HTMLElement | null = el; parent; parent = parent.parentElement) {
    const z = Number.parseInt(getComputedStyle(parent).zIndex, 10)
    if (Number.isFinite(z)) top = Math.max(top, z + 1)
  }
  return top
}

function isVisibleTarget(el: HTMLElement): boolean {
  if (!el.isConnected || el.hidden || el.getAttribute('aria-hidden') === 'true') return false
  if (el instanceof HTMLButtonElement && el.disabled) return false
  if (el.getAttribute('aria-disabled') === 'true') return false
  const style = window.getComputedStyle(el)
  if (style.display === 'none' || style.visibility === 'hidden' || style.pointerEvents === 'none') return false
  const rect = el.getBoundingClientRect()
  return rect.width > 0 && rect.height > 0
}

/** 从 targetHint 提取站内路由(/projects、/admin/users 等)。 */
function routeFromHint(hint?: string | null): string {
  if (!hint) return ''
  const m = hint.match(/(^|\s)(\/[a-z0-9][a-z0-9/_-]*(?:\?[a-z0-9%&=_.~-]+)?(?:#[a-z0-9%_.~-]+)?)/i)
  return m?.[2] ?? ''
}

function routePathname(route: string): string {
  try {
    return new URL(route, window.location.origin).pathname
  } catch {
    return route.split(/[?#]/, 1)[0]
  }
}

function currentFullPath(): string {
  return `${window.location.pathname}${window.location.search}${window.location.hash}`
}

function isExactCurrentRoute(route: string): boolean {
  try {
    const target = new URL(route, window.location.origin)
    const targetFullPath = `${target.pathname}${target.search}${target.hash}`
    return currentFullPath() === targetFullPath
  } catch {
    return false
  }
}

/** 路由 → 侧边栏中文标签(与 router/菜单文案对齐)。 */
const ROUTE_LABELS: Array<[string, string]> = [
  ['/admin/governance', 'Agent治理'],
  ['/admin/operations', '运行与审计'],
  ['/admin/access', '用户与权限'],
  ['/admin/platform', '平台配置'],
  ['/projects', '项目'],
  ['/code', '代码'],
  ['/reviews', '审查'],
  ['/issues', '问题'],
  ['/reports', '报告'],
  ['/audit', '操作审计'],
  ['/security', '安全'],
  ['/sandboxes', '沙箱'],
  ['/agents', 'Agent'],
  ['/knowledge', '知识库'],
  ['/forum', '论坛'],
]
function routeLabel(route: string): string {
  const pathname = routePathname(route)
  const exact = ROUTE_LABELS.find(([prefix]) => pathname === prefix || pathname.startsWith(`${prefix}/`))
  return exact?.[1] ?? ''
}

/** 当前是否已在目标路由(避免在同页反复找导航)。 */
function onRoute(route: string): boolean {
  const pathname = routePathname(route)
  const current = window.location.pathname
  if (current === pathname || current.startsWith(`${pathname}/`)) return true
  // 管理端路由前缀更细: /admin/xxx 只精确匹配同前缀
  if (pathname.startsWith('/admin/')) return current.startsWith(pathname)
  // 用户端列表/详情视为同区: /projects/5 视为在 /projects
  const head = pathname.split('/').slice(0, 2).join('/')
  return current.startsWith(head)
}

/**
 * 在当前页面找目标元素:
 * - hint 有路由且不在该页 → 找侧边导航中指向该路由的菜单项(点击可跳转)
 * - 已在目标页/无固定目标 → 不猜测业务按钮，等待执行团队与真实页面读取结果
 */
function findTarget(): Candidate | null {
  if (browserTarget && store.current?.key === browserActivityKey && isVisibleTarget(browserTarget)) {
    return { el: browserTarget }
  }
  const hint = store.current?.targetHint
  const route = requestedNavigation?.route || routeFromHint(hint)

  // 显式导航必须忠实执行用户刚点击的小菱入口。优先点当前页面上与目标
  // 完全一致的真实导航按钮；查询参数、锚点和详情页没有对应菜单时，
  // 就在原始小菱按钮上展示点击并调用已鉴权的导航回调，避免误点页面主操作。
  if (requestedNavigation && route) {
    const source = requestedNavigation.sourceElement
    const targetPath = routePathname(route)
    const routeHasState = route !== targetPath
    const navButtons = [...document.querySelectorAll<HTMLElement>('[data-route]')]
      .filter(isVisibleTarget)
    const exactHit = !routeHasState
      ? navButtons.find((btn) => btn.dataset.route === targetPath)
      : undefined
    if (exactHit && !isExactCurrentRoute(route)) return { el: exactHit, route }
    if (source && isVisibleTarget(source)) {
      return { el: source, route, activate: requestedNavigation.execute }
    }
  }

  if (route && !onRoute(route)) {
    const navButtons = [...document.querySelectorAll<HTMLElement>('[data-route]')]
      .filter(isVisibleTarget)
    const targetLabel = routeLabel(route)
    const exactHit = navButtons.find((btn) => btn.dataset.route === route)
    if (exactHit) return { el: exactHit, route }

    const hit = navButtons.find((btn) => {
      const text = (btn.textContent || '').trim()
      if (!text) return false
      return Boolean(targetLabel) && text.includes(targetLabel as string)
    })
    if (hit) return { el: hit, route }

    // 页面没有同路由菜单时，在用户点下的导航按钮上展示点击反馈，
    // 再执行已经由 AgentNavLink 权限校验过的回调，避免递归触发 click。
    if (
      requestedNavigation
      && requestedNavigation.route === route
      && requestedNavigation.sourceElement
      && isVisibleTarget(requestedNavigation.sourceElement)
    ) {
      return {
        el: requestedNavigation.sourceElement,
        route,
        activate: requestedNavigation.execute,
      }
    }
  }

  // 业务工具本身已经执行请求；没有固定 UI 动作标识时不能猜测页面按钮，
  // 否则会把论坛回复/查询错误呈现成“创建项目”，甚至重复提交写操作。
  return null
}

function centerOf(el: HTMLElement): { x: number; y: number; box: { left: number; top: number; width: number; height: number } } {
  const rect = el.getBoundingClientRect()
  return {
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
    box: { left: rect.left, top: rect.top, width: rect.width, height: rect.height },
  }
}

function isPromiseLike(value: unknown): value is Promise<unknown> {
  return Boolean(value && typeof (value as { then?: unknown }).then === 'function')
}

function waitForRoute(route: string, timeoutMs = 1200): Promise<boolean> {
  if (isExactCurrentRoute(route)) return Promise.resolve(true)
  return new Promise((resolve) => {
    const startedAt = Date.now()
    const check = () => {
      if (isExactCurrentRoute(route)) {
        resolve(true)
        return
      }
      if (Date.now() - startedAt >= timeoutMs) {
        resolve(false)
        return
      }
      window.setTimeout(check, 50)
    }
    window.setTimeout(check, 50)
  })
}

async function activateTarget(target: Candidate, sequence: number): Promise<boolean> {
  try {
    // 定位后、动画到达前页面可能弹出对话框，点击前再次检查实际命中。
    if (target.el !== document.body && (!isVisibleTarget(target.el) || !isUnobscuredTarget(target.el))) return false
    const result = target.activate ? target.activate() : target.el.click()
    if (isPromiseLike(result)) await result
    if (sequence !== activitySequence) return false
    if (!target.route) return true
    return await waitForRoute(target.route)
  } catch {
    return false
  }
}

/** 将侧栏折叠区或长页面中的真实目标带入视区，保证用户能看到鼠标落点。 */
function bringTargetIntoView(el: HTMLElement): void {
  const rect = el.getBoundingClientRect()
  const margin = 24
  const outsideViewport = rect.bottom < margin
    || rect.top > window.innerHeight - margin
    || rect.right < margin
    || rect.left > window.innerWidth - margin
  if (!outsideViewport) return
  if (typeof el.scrollIntoView === 'function') {
    el.scrollIntoView({ behavior: 'auto', block: 'center', inline: 'nearest' })
  }
}

function fallbackPoint(): { x: number; y: number } {
  const el = document.querySelector('.app-main, .layout-main, main, #app')
  const rect = el?.getBoundingClientRect()
  if (rect && rect.width > 0) {
    return {
      x: Math.min(Math.max(rect.left + rect.width / 2, 40), window.innerWidth - 40),
      y: Math.min(Math.max(rect.top + Math.min(rect.height / 2, window.innerHeight * 0.45), 40), window.innerHeight - 40),
    }
  }
  return { x: window.innerWidth / 2, y: window.innerHeight / 2 }
}

function moveToTarget(): void {
  const sequence = activitySequence
  navigated = false
  clearHalo()
  navigationResult.value = null
  // 先钉在右下角(无过渡),下一帧滑向真实目标
  x.value = window.innerWidth - 72
  y.value = window.innerHeight - 72
  startRaf = window.requestAnimationFrame(() => {
    targetRaf = window.requestAnimationFrame(() => {
      if (sequence !== activitySequence) return
      const target = findTarget()
      let point: { x: number; y: number }
      if (target) {
        if (!isVisibleTarget(target.el)) return
        bringTargetIntoView(target.el)
        if (!isUnobscuredTarget(target.el)) {
          visible.value = false
          if (requestedNavigation) finishRequestedNavigation(false)
          return
        }
        const c = centerOf(target.el)
        cursorLayerZ.value = layerForTarget(target.el)
        point = { x: c.x, y: c.y }
        targetBox.value = c.box
        lastTargetEl = target.el
        target.el.classList.add('xl-vcursor-target')
        arriveTimer = window.setTimeout(() => {
          arrived.value = true
          // 目标是其他页面的导航项:涟漪节奏后真实点击完成跳转
          if (target.route) {
            clickTimer = window.setTimeout(() => {
              if (navigated || sequence !== activitySequence) return
              navigated = true
              clicking.value = true
              void activateTarget(target, sequence).then((success) => {
                if (sequence !== activitySequence) return
                navigationResult.value = success ? 'success' : 'failure'
                clicking.value = false
                clickStateTimer = window.setTimeout(() => {
                  clicking.value = false
                }, 260)
                finishRequestedNavigation(success)
              })
            }, 650)
          }
        }, 700)
      } else {
        // API 排队、后台核验等阶段没有 DOM 点击目标：只保留工作状态，
        // 不把鼠标停在任意卡片/按钮中央，避免制造实际点击的假象。
        if (!requestedNavigation) {
          visible.value = false
          return
        }
        point = fallbackPoint()
        targetBox.value = null
        arriveTimer = window.setTimeout(() => {
          arrived.value = true
          if (requestedNavigation) {
            clickTimer = window.setTimeout(() => {
              if (navigated || !requestedNavigation || sequence !== activitySequence) return
              navigated = true
              clicking.value = true
              const requested = requestedNavigation
              void activateTarget({
                el: requested.sourceElement || document.body,
                route: requested.route,
                activate: requested.execute,
              }, sequence).then((success) => {
                if (sequence !== activitySequence) return
                navigationResult.value = success ? 'success' : 'failure'
                clicking.value = false
                clickStateTimer = window.setTimeout(() => {
                  clicking.value = false
                }, 260)
                finishRequestedNavigation(success)
              })
            }, 650)
          }
        }, 700)
      }
      x.value = point.x
      y.value = point.y
    })
  })
}

/** Browser bridge 负责真实动作；本事件仅可视化已定位的控件，绝不重复点击。 */
function handleBrowserTarget(event: Event): void {
  const detail = (event as CustomEvent<{ element?: HTMLElement; action?: string; label?: string }>).detail
  if (!(detail?.element instanceof HTMLElement) || !isVisibleTarget(detail.element)) return
  if (!['click', 'fill', 'select', 'check', 'scroll'].includes(detail.action || '')) return
  // 在开始显示光标前就进入目标控件所在层级；等双 RAF 定位后再改会让首帧落在弹层后面。
  cursorLayerZ.value = layerForTarget(detail.element)
  if (browserActivityKey) store.end(browserActivityKey)
  browserTarget = detail.element
  browserActivityKey = `site-browser-visual-${Date.now()}-${Math.random().toString(16).slice(2)}`
  store.begin(detail.label || '小菱正在操作已定位控件', browserActivityKey)
  store.complete(browserActivityKey, 1400)
}

function finishRequestedNavigation(_success: boolean): void {
  if (requestedActivityKey) store.complete(requestedActivityKey, 900)
  requestedNavigation = null
  requestedActivityKey = ''
}

/** 接收已通过组件权限校验的导航请求，并启动全局可视化点击。 */
function handleNavigationRequest(event: Event): void {
  const detail = (event as CustomEvent<XiaolingNavigationRequest>).detail
  if (!detail || !isSafeXiaolingRoute(detail.route) || typeof detail.execute !== 'function') return
  detail.handled = true
  if (requestedActivityKey) store.end(requestedActivityKey)
  requestedNavigation = detail
  requestedActivityKey = store.begin(
    `小菱正在打开${detail.label}…`,
    `xiaoling-nav-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    `${detail.route} ${detail.label}`,
  )
}

function clearHalo(): void {
  lastTargetEl?.classList.remove('xl-vcursor-target')
  lastTargetEl = null
}

function syncTargetGeometry(): void {
  if (!visible.value || !lastTargetEl) return
  if (!isVisibleTarget(lastTargetEl) || !isUnobscuredTarget(lastTargetEl)) {
    targetBox.value = null
    visible.value = false
    clearHalo()
    return
  }
  const point = centerOf(lastTargetEl)
  x.value = point.x
  y.value = point.y
  targetBox.value = point.box
}

watch(
  () => [store.isActing, store.current?.key] as const,
  ([acting]) => {
    activitySequence += 1
    window.clearTimeout(arriveTimer)
    window.clearTimeout(clickTimer)
    window.clearTimeout(haloTimer)
    window.clearTimeout(clickStateTimer)
    window.cancelAnimationFrame(startRaf)
    window.cancelAnimationFrame(targetRaf)
    if (acting) {
      arrived.value = false
      clicking.value = false
      navigationResult.value = null
      visible.value = true
      void nextTick(moveToTarget)
    } else {
      visible.value = false
      arrived.value = false
      clicking.value = false
      targetBox.value = null
      requestedNavigation = null
      requestedActivityKey = ''
      browserTarget = null
      browserActivityKey = ''
      haloTimer = window.setTimeout(clearHalo, 400)
    }
  },
)

onMounted(() => {
  window.addEventListener(XIAOLING_NAVIGATION_EVENT, handleNavigationRequest)
  window.addEventListener('prism:site-browser-target', handleBrowserTarget)
  window.addEventListener('resize', syncTargetGeometry)
  window.addEventListener('scroll', syncTargetGeometry, true)
})

onBeforeUnmount(() => {
  window.clearTimeout(arriveTimer)
  window.clearTimeout(clickTimer)
  window.clearTimeout(haloTimer)
  window.clearTimeout(clickStateTimer)
  window.cancelAnimationFrame(startRaf)
  window.cancelAnimationFrame(targetRaf)
  window.removeEventListener(XIAOLING_NAVIGATION_EVENT, handleNavigationRequest)
  window.removeEventListener('prism:site-browser-target', handleBrowserTarget)
  window.removeEventListener('resize', syncTargetGeometry)
  window.removeEventListener('scroll', syncTargetGeometry, true)
  if (browserActivityKey) store.end(browserActivityKey)
  if (requestedActivityKey) store.end(requestedActivityKey)
  activitySequence += 1
  requestedNavigation = null
  requestedActivityKey = ''
  clearHalo()
})
</script>

<template>
  <Transition name="virtual-cursor-fade">
    <div v-if="visible" class="virtual-cursor-layer" :style="{ zIndex: cursorLayerZ }" aria-hidden="true">
      <!-- 目标元素聚焦光晕(定位到真实元素矩形) -->
      <div v-if="arrived && targetBox" class="virtual-cursor-halo" :style="haloStyle"></div>
      <div class="virtual-cursor" :class="{ 'is-arrived': arrived, 'is-clicking': clicking }" :style="cursorStyle">
        <span v-if="arrived" class="virtual-cursor-ripple"></span>
        <span v-if="arrived" class="virtual-cursor-ripple is-delayed"></span>
        <span v-if="clicking" class="virtual-cursor-click-label is-pending">点击中</span>
        <span v-else-if="navigationResult === 'success'" class="virtual-cursor-click-label">已打开</span>
        <span v-else-if="navigationResult === 'failure'" class="virtual-cursor-click-label is-failure">跳转失败</span>
        <svg class="virtual-cursor-icon" width="26" height="26" viewBox="0 0 26 26" fill="none">
          <defs>
            <linearGradient id="virtual-cursor-g" x1="3" y1="3" x2="23" y2="23" gradientUnits="userSpaceOnUse">
              <stop offset="0" stop-color="#8E88F5" />
              <stop offset="0.6" stop-color="#5B58E8" />
              <stop offset="1" stop-color="#3DBCD9" />
            </linearGradient>
          </defs>
          <path
            d="M4 3.5 L21.5 12.2 L13.8 14.6 L10.4 22.2 Z"
            fill="url(#virtual-cursor-g)"
            stroke="#FFFFFF"
            stroke-width="1.4"
            stroke-linejoin="round"
          />
          <circle cx="20" cy="5" r="1.6" fill="#FFD66E" />
          <circle cx="23" cy="9.5" r="1.1" fill="#7EE3F0" />
        </svg>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.virtual-cursor-layer {
  position: fixed;
  inset: 0;
  z-index: var(--layer-agent-cursor);
  pointer-events: none;
}

.virtual-cursor {
  position: absolute;
  left: 0;
  top: 0;
  transition: transform 0.65s cubic-bezier(0.22, 0.9, 0.32, 1);
  will-change: transform;
}

.virtual-cursor-icon {
  display: block;
  filter: drop-shadow(0 2px 8px rgba(91, 88, 232, 0.5));
}

/* 到达目标后的轻微悬停浮动 */
.virtual-cursor.is-arrived .virtual-cursor-icon {
  animation: virtual-cursor-hover 1.6s ease-in-out infinite;
}

.virtual-cursor.is-clicking .virtual-cursor-icon {
  animation: virtual-cursor-click 0.26s ease-out;
}

.virtual-cursor-click-label {
  position: absolute;
  left: 22px;
  top: 22px;
  padding: 3px 7px;
  border: 1px solid rgba(91, 88, 232, 0.22);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.94);
  box-shadow: 0 4px 12px rgba(31, 35, 41, 0.16);
  color: var(--brand-600, #5b58e8);
  font-size: 10px;
  font-weight: 650;
  white-space: nowrap;
  animation: virtual-cursor-click-label 0.26s ease-out both;
}

.virtual-cursor-click-label.is-pending {
  border-color: rgba(194, 139, 44, 0.3);
  color: #9a6810;
}

.virtual-cursor-click-label.is-failure {
  border-color: rgba(201, 42, 78, 0.28);
  color: #b4234d;
}

/* 目标元素聚焦光晕:贴住真实元素矩形,呼吸发亮 */
.virtual-cursor-halo {
  position: absolute;
  border-radius: 10px;
  border: 2px solid var(--brand-400, #8E88F5);
  box-shadow:
    0 0 0 4px rgba(91, 88, 232, 0.15),
    0 0 18px rgba(91, 88, 232, 0.35) inset,
    0 0 22px rgba(91, 88, 232, 0.25);
  animation: virtual-cursor-halo 1.4s ease-in-out infinite;
  pointer-events: none;
}
@keyframes virtual-cursor-halo {
  0%, 100% { opacity: 0.55; }
  50% { opacity: 1; }
}

/* 目标元素本体高亮(JS 加类) */
:global(.xl-vcursor-target) {
  outline: 2px solid rgba(91, 88, 232, 0.55) !important;
  outline-offset: 2px;
}

/* 目标高亮涟漪 */
.virtual-cursor-ripple {
  position: absolute;
  left: 8px;
  top: 8px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  border: 2px solid var(--brand-400);
  opacity: 0;
  animation: virtual-cursor-ripple 1.8s ease-out infinite;
}
.virtual-cursor-ripple.is-delayed {
  animation-delay: 0.9s;
}

.virtual-cursor-fade-enter-active,
.virtual-cursor-fade-leave-active {
  transition: opacity var(--transition-base);
}
.virtual-cursor-fade-enter-from,
.virtual-cursor-fade-leave-to {
  opacity: 0;
}

@keyframes virtual-cursor-ripple {
  0% { transform: translate(-50%, -50%) scale(0.6); opacity: 0.75; }
  100% { transform: translate(-50%, -50%) scale(3.2); opacity: 0; }
}

@keyframes virtual-cursor-hover {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-2px); }
}

@keyframes virtual-cursor-click {
  0% { transform: scale(1); }
  45% { transform: scale(0.78); }
  100% { transform: scale(1); }
}

@keyframes virtual-cursor-click-label {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

@media (prefers-reduced-motion: reduce) {
  .virtual-cursor { transition: none; }
  .virtual-cursor.is-arrived .virtual-cursor-icon { animation: none; }
  .virtual-cursor.is-clicking .virtual-cursor-icon { animation: none; }
  .virtual-cursor-ripple { animation: none; opacity: 0.35; transform: translate(-50%, -50%) scale(1.6); }
  .virtual-cursor-halo { animation: none; opacity: 0.6; }
}
</style>
