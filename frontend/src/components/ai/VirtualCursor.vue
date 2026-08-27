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
/** 目标元素屏幕矩形,用于定位涟漪与聚焦光晕 */
const targetBox = ref<{ left: number; top: number; width: number; height: number } | null>(null)
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
let startRaf = 0
/** 已在本次活动中触发过导航,避免重复跳转 */
let navigated = false
/** 上一个被加高亮类的目标元素,结束时移除 */
let lastTargetEl: HTMLElement | null = null
/** 导航确认按钮发出的单次可视化导航请求。 */
let requestedNavigation: XiaolingNavigationRequest | null = null
let requestedActivityKey = ''

interface Candidate {
  el: HTMLElement
  route?: string
  activate?: () => void
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
  ['/admin/overview', '总览'],
  ['/admin/users', '用户'],
  ['/admin/approvals', '审批'],
  ['/admin/observability', '监控'],
  ['/admin/audit', '审计'],
  ['/admin/jobs', '任务调度'],
  ['/admin/rollback', '回滚'],
  ['/admin/ai-logs', '调用日志'],
  ['/admin/mcp-workers', '节点'],
  ['/projects', '项目'],
  ['/code', '代码'],
  ['/reviews', '审查'],
  ['/issues', '问题'],
  ['/reports', '报告'],
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
 * - 已在目标页/无路由 → 找页面主操作按钮(创建/发起等)或与 hint 语义匹配的按钮
 */
function findTarget(): Candidate | null {
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
    const exactHit = !routeHasState
      ? navButtons.find((btn) => btn.dataset.route === targetPath)
      : undefined
    if (exactHit && !isExactCurrentRoute(route)) return { el: exactHit, route }
    if (source?.isConnected) {
      return { el: source, route, activate: requestedNavigation.execute }
    }
  }

  if (route && !onRoute(route)) {
    const navButtons = [...document.querySelectorAll<HTMLElement>('[data-route]')]
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
      && requestedNavigation.sourceElement?.isConnected
    ) {
      return {
        el: requestedNavigation.sourceElement,
        route,
        activate: requestedNavigation.execute,
      }
    }
  }

  // 当前页内的行动点:先按 hint 中文词匹配,再兜底主操作按钮
  const actionWords = (hint?.match(/[一-龥]{2,6}/g) ?? []).filter((w) => !routeLabel(route) || !(hint ?? '').includes(w + '页'))
  const pageButtons = [...document.querySelectorAll<HTMLElement>('main button, .app-main button, [class*=content] button')]
  const byHint = pageButtons.find((btn) => {
    const text = (btn.textContent || '').trim()
    return Boolean(text) && actionWords.some((w) => w.length >= 2 && text.includes(w))
  })
  if (byHint) return { el: byHint }

  const primary = pageButtons.find((btn) => {
    const text = (btn.textContent || '').trim()
    const disabled = (btn as HTMLButtonElement).disabled === true
    return /创建|新建|发起|上传|添加|导出/.test(text) && !disabled
  })
  if (primary) return { el: primary }
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
  navigated = false
  clearHalo()
  // 先钉在右下角(无过渡),下一帧滑向真实目标
  x.value = window.innerWidth - 72
  y.value = window.innerHeight - 72
  startRaf = window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => {
      const target = findTarget()
      let point: { x: number; y: number }
      if (target) {
        bringTargetIntoView(target.el)
        const c = centerOf(target.el)
        point = { x: c.x, y: c.y }
        targetBox.value = c.box
        lastTargetEl = target.el
        target.el.classList.add('xl-vcursor-target')
        arriveTimer = window.setTimeout(() => {
          arrived.value = true
          // 目标是其他页面的导航项:涟漪节奏后真实点击完成跳转
          if (target.route) {
            clickTimer = window.setTimeout(() => {
              if (navigated) return
              navigated = true
              if (target.activate) target.activate()
              else target.el.click()
              finishRequestedNavigation()
            }, 650)
          }
        }, 700)
      } else {
        point = fallbackPoint()
        targetBox.value = null
        arriveTimer = window.setTimeout(() => {
          arrived.value = true
          if (requestedNavigation) {
            clickTimer = window.setTimeout(() => {
              if (navigated || !requestedNavigation) return
              navigated = true
              requestedNavigation.execute()
              finishRequestedNavigation()
            }, 650)
          }
        }, 700)
      }
      x.value = point.x
      y.value = point.y
    })
  })
}

function finishRequestedNavigation(): void {
  if (requestedActivityKey) store.complete(requestedActivityKey, 420)
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

watch(
  () => [store.isActing, store.current?.key] as const,
  ([acting]) => {
    window.clearTimeout(arriveTimer)
    window.clearTimeout(clickTimer)
    window.clearTimeout(haloTimer)
    window.cancelAnimationFrame(startRaf)
    if (acting) {
      arrived.value = false
      visible.value = true
      void nextTick(moveToTarget)
    } else {
      visible.value = false
      arrived.value = false
      targetBox.value = null
      haloTimer = window.setTimeout(clearHalo, 400)
    }
  },
)

onMounted(() => {
  window.addEventListener(XIAOLING_NAVIGATION_EVENT, handleNavigationRequest)
})

onBeforeUnmount(() => {
  window.clearTimeout(arriveTimer)
  window.clearTimeout(clickTimer)
  window.clearTimeout(haloTimer)
  window.cancelAnimationFrame(startRaf)
  window.removeEventListener(XIAOLING_NAVIGATION_EVENT, handleNavigationRequest)
  if (requestedActivityKey) store.end(requestedActivityKey)
  requestedNavigation = null
  requestedActivityKey = ''
  clearHalo()
})
</script>

<template>
  <Transition name="virtual-cursor-fade">
    <div v-if="visible" class="virtual-cursor-layer" aria-hidden="true">
      <!-- 目标元素聚焦光晕(定位到真实元素矩形) -->
      <div v-if="arrived && targetBox" class="virtual-cursor-halo" :style="haloStyle"></div>
      <div class="virtual-cursor" :class="{ 'is-arrived': arrived }" :style="cursorStyle">
        <span v-if="arrived" class="virtual-cursor-ripple"></span>
        <span v-if="arrived" class="virtual-cursor-ripple is-delayed"></span>
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
  z-index: calc(var(--z-index-message) + 200);
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

@media (prefers-reduced-motion: reduce) {
  .virtual-cursor { transition: none; }
  .virtual-cursor.is-arrived .virtual-cursor-icon { animation: none; }
  .virtual-cursor-ripple { animation: none; opacity: 0.35; transform: translate(-50%, -50%) scale(1.6); }
  .virtual-cursor-halo { animation: none; opacity: 0.6; }
}
</style>
