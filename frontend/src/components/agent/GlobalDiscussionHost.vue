<template>
  <Teleport :to="assistantSlotTarget || 'body'" :disabled="!assistantSlotTarget">
    <div v-if="sessions.length || selected || listError" ref="dockRef" class="roundtable-dock" :class="assistantSlotTarget ? 'is-assistant' : 'is-unassigned'">
      <div v-if="listError" class="roundtable-dock-error" role="status">
        {{ listError }}
        <button type="button" @click="loadSessions">重试</button>
      </div>
      <Teleport to="body" :disabled="!assistantSlotTarget">
      <div v-if="(sessions.length > 1 || nextOffset !== null) && showChoices && !visible" ref="choicesRef" class="roundtable-choices" :class="{ 'is-assistant-popup': assistantSlotTarget }" :style="assistantSlotTarget ? choicesStyle : undefined" role="dialog" aria-label="我的圆桌讨论">
        <button
          v-for="session in sessions"
          :key="session.session_id"
          type="button"
          class="roundtable-choice"
          @click="openSession(session.session_id)"
        >
          <span>{{ session.file_name || '未命名文件' }}</span>
          <small>{{ statusLabel(session.status, session.progress?.phase, session.followup_until) }}</small>
        </button>
        <button v-if="nextOffset !== null" type="button" class="roundtable-load-more" :disabled="moreLoading" @click="loadMore">
          {{ moreLoading ? '正在加载…' : '加载更多圆桌' }}
        </button>
      </div>
      </Teleport>
      <button
        ref="triggerRef"
        v-if="!visible && (sessions.length || selected)"
        class="roundtable-fab"
        :class="{ 'is-assistant': assistantSlotTarget }"
        type="button"
        aria-label="打开圆桌讨论"
        aria-haspopup="dialog"
        :aria-expanded="showChoices"
        :title="`我的圆桌讨论：可继续 ${activeCount} 个，已结束 ${sessions.length - activeCount} 个`"
        @click="openFromDock"
      >
        <svg class="roundtable-icon" viewBox="0 0 20 20" fill="none" aria-hidden="true">
          <path d="M3.25 4.75A2.25 2.25 0 0 1 5.5 2.5h9A2.25 2.25 0 0 1 16.75 4.75v5A2.25 2.25 0 0 1 14.5 12H8l-4.75 3v-10.25Z" stroke="currentColor" stroke-width="1.45" stroke-linejoin="round" />
          <path d="M6.5 6.5h7M6.5 9h4.5" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" />
        </svg>
        <span class="roundtable-label">圆桌</span>
        <span v-if="activeCount > 0" class="roundtable-count" aria-label="可继续的圆桌数量">{{ activeCount }}{{ nextOffset !== null ? '+' : '' }}</span>
      </button>
    </div>
  </Teleport>

  <AgentDiscussionPanel
    v-if="visible && selected"
    :key="selected.session_id"
    :session-id="selected.session_id"
    :ws-url="selected.ws_url"
    :agents="selected.agents"
    :file-name="selected.file_name"
    :initial-progress="selected.progress"
    :initial-status="selected.status"
    :initial-report-task-id="selected.report_task_id"
    :initial-followup-until="selected.followup_until"
    :initial-turns="selected.turns"
    :initial-has-earlier="selected.has_earlier"
    :initial-next-before-seq="selected.next_before_seq"
    @close="closePanel"
    @settled="loadSessions"
  />
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import AgentDiscussionPanel from './AgentDiscussionPanel.vue'
import {
  getDiscussionSession,
  listDiscussionSessions,
  type DiscussionSessionDetail,
  type DiscussionSessionSummary,
} from '@/api/discussion'

const props = defineProps<{ userId: number }>()
const route = useRoute()
const sessions = ref<DiscussionSessionSummary[]>([])
const selected = ref<DiscussionSessionDetail | null>(null)
const visible = ref(false)
const showChoices = ref(false)
const dockRef = ref<HTMLElement | null>(null)
const triggerRef = ref<HTMLButtonElement | null>(null)
const choicesRef = ref<HTMLElement | null>(null)
const choicesStyle = ref<Record<string, string>>({})
const listError = ref('')
const nextOffset = ref<number | null>(null)
const moreLoading = ref(false)
const assistantSlotTarget = ref('')
const followupClock = ref(Date.now())
let authGeneration = 0
let listGeneration = 0
let detailGeneration = 0
let refreshTimer: ReturnType<typeof setInterval> | null = null
let followupTimer: ReturnType<typeof setInterval> | null = null
let headerSlotObserver: MutationObserver | null = null

const routeSessionId = computed(() => {
  const raw = route.query.discuss_session
  return typeof raw === 'string' ? raw : Array.isArray(raw) ? raw[0] || '' : ''
})
const activeCount = computed(() => sessions.value.filter((session) => canContinue(session)).length)

function followupOpen(session: Pick<DiscussionSessionSummary, 'status' | 'followup_until'>, now = followupClock.value / 1000): boolean {
  return session.status === 'concluded' && Number(session.followup_until) > now
}

function canContinue(session: DiscussionSessionSummary): boolean {
  return ['active', 'running', 'paused'].includes(session.status) || followupOpen(session)
}

function syncFollowupTimer(): void {
  const hasOpenWindow = sessions.value.some((session) => followupOpen(session))
  if (hasOpenWindow && !followupTimer) {
    followupTimer = setInterval(() => {
      followupClock.value = Date.now()
      if (!sessions.value.some((session) => followupOpen(session))) {
        if (followupTimer) clearInterval(followupTimer)
        followupTimer = null
      }
    }, 1000)
  } else if (!hasOpenWindow && followupTimer) {
    clearInterval(followupTimer)
    followupTimer = null
  }
}

/** 列表与详情均由服务端按登录身份过滤；迟到响应不得覆盖切换后的账号。 */
async function loadSessions(): Promise<void> {
  const generation = ++listGeneration
  const auth = authGeneration
  const owner = props.userId
  try {
    const page = await listDiscussionSessions()
    if (generation !== listGeneration || auth !== authGeneration || owner !== props.userId) return
    sessions.value = Array.isArray(page?.items) ? page.items : []
    followupClock.value = Date.now()
    syncFollowupTimer()
    nextOffset.value = typeof page?.next_offset === 'number' ? page.next_offset : null
    listError.value = ''
  } catch {
    if (generation !== listGeneration || auth !== authGeneration || owner !== props.userId) return
    listError.value = '圆桌列表暂时无法读取'
  }
}

async function loadMore(): Promise<void> {
  if (nextOffset.value === null || moreLoading.value) return
  const generation = listGeneration
  const auth = authGeneration
  const owner = props.userId
  const offset = nextOffset.value
  moreLoading.value = true
  try {
    const page = await listDiscussionSessions(30, offset)
    if (generation !== listGeneration || auth !== authGeneration || owner !== props.userId) return
    const known = new Set(sessions.value.map((session) => session.session_id))
    sessions.value = [...sessions.value, ...(page.items || []).filter((session) => !known.has(session.session_id))]
    followupClock.value = Date.now()
    syncFollowupTimer()
    nextOffset.value = typeof page.next_offset === 'number' ? page.next_offset : null
    listError.value = ''
  } catch {
    if (generation === listGeneration && auth === authGeneration && owner === props.userId) {
      listError.value = '更多圆桌暂时无法读取'
    }
  } finally {
    // 旧账号的迟到请求不能清除新账号分页请求的加载状态。
    // 同账号列表刷新会使分页结果过期，但仍必须解除该分页请求占用的 loading；
    // 账号切换时则由新账号请求持有状态，旧账号 finally 不得清理它。
    if (auth === authGeneration && owner === props.userId) {
      moreLoading.value = false
    }
  }
}

async function openSession(sessionId: string): Promise<void> {
  const owner = props.userId
  const auth = authGeneration
  const generation = ++detailGeneration
  try {
    const detail = await getDiscussionSession(sessionId)
    if (owner !== props.userId || auth !== authGeneration || generation !== detailGeneration) return
    if (!detail || detail.session_id !== sessionId) return
    selected.value = { ...detail, agents: Array.isArray(detail.agents) ? detail.agents : [] }
    visible.value = true
    showChoices.value = false
    listError.value = ''
  } catch {
    if (owner !== props.userId || auth !== authGeneration || generation !== detailGeneration) return
    listError.value = '圆桌会话已失效或当前账号无权查看'
  }
}

function closePanel(): void {
  detailGeneration++
  visible.value = false
}

/** 菜单脱离玻璃页头的 stacking context；坐标以触发按钮与视口为准。 */
function positionChoices(): void {
  if (!showChoices.value || !assistantSlotTarget.value || !triggerRef.value) return
  const box = triggerRef.value.getBoundingClientRect()
  const width = Math.min(300, Math.max(0, window.innerWidth - 24))
  const left = Math.max(12, Math.min(box.right - width, window.innerWidth - width - 12))
  const top = Math.max(12, Math.min(box.bottom + 8, window.innerHeight - 60))
  choicesStyle.value = {
    left: `${left}px`, top: `${top}px`, width: `${width}px`,
    maxHeight: `${Math.max(40, Math.min(320, window.innerHeight - top - 12))}px`,
  }
}

function closeChoices(restoreFocus = false): void {
  if (!showChoices.value) return
  showChoices.value = false
  detailGeneration++
  if (restoreFocus) triggerRef.value?.focus()
}

function stackingLayer(element: HTMLElement): number {
  let layer = 0
  for (let node: HTMLElement | null = element; node && node !== document.body; node = node.parentElement) {
    const parsed = Number.parseInt(getComputedStyle(node).zIndex, 10)
    if (Number.isFinite(parsed)) layer = Math.max(layer, parsed)
  }
  return layer
}

function dialogIsVisible(element: HTMLElement): boolean {
  if (!element.isConnected || element.closest('[hidden],[aria-hidden="true"],[inert]')
    || element.getClientRects().length === 0) return false
  const visibility = getComputedStyle(element).visibility
  if (visibility === 'hidden' || visibility === 'collapse') return false
  // 已关闭的 Element Plus overlay 仍保留 display:block 的 dialog 子节点。
  // 只有自身与祖先都实际渲染的模态才能优先消费 Escape。
  // visibility 可被后代显式覆盖，因此只判断 dialog 最终的计算值。
  for (let node: HTMLElement | null = element; node; node = node.parentElement) {
    if (getComputedStyle(node).display === 'none') return false
  }
  return true
}

function hasHigherDialogThanChoices(): boolean {
  const menu = choicesRef.value
  if (!menu) return false
  const menuLayer = stackingLayer(menu)
  return Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"],.el-message-box,[aria-modal="true"]'))
    .some(dialog => dialog !== menu && !menu.contains(dialog)
      && dialogIsVisible(dialog)
      && stackingLayer(dialog) > menuLayer)
}

function onOutsideClick(event: Event): void {
  const target = event.target
  if (!(target instanceof Node) || dockRef.value?.contains(target) || choicesRef.value?.contains(target)) return
  closeChoices()
}

function onMenuKeydown(event: KeyboardEvent): void {
  if (!showChoices.value || event.key !== 'Escape' || hasHigherDialogThanChoices()) return
  event.preventDefault()
  // 菜单只有在没有更高模态层时才消费 Escape 并恢复触发按钮焦点。
  event.stopImmediatePropagation()
  closeChoices(true)
}

watch(showChoices, async (open) => {
  if (!open) return
  positionChoices()
  await nextTick()
  positionChoices()
  choicesRef.value?.querySelector<HTMLButtonElement>('button')?.focus()
})
watch(() => route.fullPath, () => {
  closeChoices()
  syncAssistantSlot()
})
watch(assistantSlotTarget, (target) => {
  if (!target) closeChoices()
})

function openFromDock(): void {
  if (sessions.value.length === 1 && nextOffset.value === null) {
    void openSession(sessions.value[0].session_id)
    return
  }
  if (sessions.value.length === 0 && selected.value) {
    void openSession(selected.value.session_id)
    return
  }
  showChoices.value = !showChoices.value
  if (!showChoices.value) detailGeneration++
}

function statusLabel(status: string, progressPhase?: string, followupUntil?: number): string {
  if (status === 'concluded' && Number(followupUntil) > followupClock.value / 1000) return '追问中'
  if (progressPhase === 'partial') return '部分完成'
  if (progressPhase === 'failed') return '失败'
  if (progressPhase === 'cancelled') return '已取消'
  if (progressPhase === 'interrupted') return '已中断'
  if (status === 'active' || status === 'running') return '进行中'
  if (status === 'paused') return '已暂停'
  if (status === 'concluded' || status === 'completed') return '已结束'
  if (status === 'interrupted') return '已中断'
  return '待查看'
}

function onOpenEvent(event: Event): void {
  const id = (event as CustomEvent<{ sessionId?: string }>).detail?.sessionId
  if (id) void openSession(id)
  else openFromDock()
}

function onVisibilityChange(): void {
  if (document.visibilityState === 'visible') void loadSessions()
}

function onRoundtableListChanged(event: Event): void {
  const owner = Number((event as CustomEvent<{ ownerUserId?: number }>).detail?.ownerUserId)
  if (owner === props.userId) void loadSessions()
}

function syncAssistantSlot(): void {
  const adminSlot = document.getElementById('roundtable-admin-assistant-slot')
  const userSlot = document.getElementById('roundtable-user-assistant-slot')
  const preferredSlot = route.path.startsWith('/admin')
    ? adminSlot || userSlot
    : userSlot || adminSlot
  assistantSlotTarget.value = preferredSlot ? `#${preferredSlot.id}` : ''
}

watch(() => props.userId, () => {
  authGeneration++
  listGeneration++
  detailGeneration++
  moreLoading.value = false
  sessions.value = []
  nextOffset.value = null
  selected.value = null
  visible.value = false
  showChoices.value = false
  listError.value = ''
  void loadSessions()
}, { immediate: true })

watch(routeSessionId, (id) => {
  if (id) void openSession(id)
}, { immediate: true })

onMounted(() => {
  // The launcher belongs inside the active Xiaoling panel. Keep this host
  // mounted at app level so a running discussion survives route changes.
  headerSlotObserver = new MutationObserver(syncAssistantSlot)
  headerSlotObserver.observe(document.body, { childList: true, subtree: true })
  syncAssistantSlot()
  document.addEventListener('visibilitychange', onVisibilityChange)
  window.addEventListener('prism:open-roundtable', onOpenEvent)
  window.addEventListener('prism:roundtable-list-changed', onRoundtableListChanged)
  document.addEventListener('click', onOutsideClick, true)
  window.addEventListener('keydown', onMenuKeydown, true)
  window.addEventListener('resize', positionChoices)
  window.addEventListener('scroll', positionChoices, true)
  // 后台工具完成事件可能发生在异步宿主挂载前；可见页低频对账兜底。
  refreshTimer = setInterval(() => {
    if (document.visibilityState === 'visible') void loadSessions()
  }, 30_000)
})
onBeforeUnmount(() => {
  authGeneration++
  listGeneration++
  detailGeneration++
  headerSlotObserver?.disconnect()
  headerSlotObserver = null
  document.removeEventListener('visibilitychange', onVisibilityChange)
  window.removeEventListener('prism:open-roundtable', onOpenEvent)
  window.removeEventListener('prism:roundtable-list-changed', onRoundtableListChanged)
  document.removeEventListener('click', onOutsideClick, true)
  window.removeEventListener('keydown', onMenuKeydown, true)
  window.removeEventListener('resize', positionChoices)
  window.removeEventListener('scroll', positionChoices, true)
  if (refreshTimer) clearInterval(refreshTimer)
  if (followupTimer) clearInterval(followupTimer)
})
</script>

<style scoped>
.roundtable-dock { display: grid; justify-items: end; gap: 8px; max-width: min(320px, calc(100vw - 24px)); }
.roundtable-dock.is-unassigned { display: none; }
.roundtable-dock.is-assistant { position: relative; display: flex; justify-items: initial; max-width: none; }
.roundtable-fab { display: inline-flex; align-items: center; gap: 8px; min-height: 44px; padding: 8px 12px; border: 1px solid #d9ddfa; border-radius: 999px; background: #fff; color: #292e67; box-shadow: 0 8px 28px #24295c33; font: inherit; font-size: 13px; font-weight: 650; cursor: pointer; }
.roundtable-fab.is-assistant { flex: 0 0 auto; min-height: 30px; padding: 0 7px; gap: 5px; border-color: transparent; border-radius: 7px; background: transparent; color: var(--color-text-tertiary, #8990a1); box-shadow: none; font-size: 11.5px; font-weight: 500; }
.roundtable-fab.is-assistant:hover { border-color: transparent; background: var(--color-fill-light, #f3f4f8); color: var(--color-text-primary, #454b5b); }
.roundtable-icon { display: block; width: 16px; height: 16px; flex: none; }
.roundtable-fab:focus-visible, .roundtable-choice:focus-visible { outline: 2px solid #5b58e8; outline-offset: 2px; }
.roundtable-count { display: inline-grid; place-items: center; min-width: 22px; height: 22px; padding: 0 4px; border-radius: 999px; background: #eef0ff; font-size: 11px; }
.roundtable-fab.is-assistant .roundtable-count { min-width: 16px; height: 16px; padding: 0 3px; background: var(--color-fill-light, #f0f1f6); color: var(--color-text-secondary, #72798a); font-size: 9px; }
.roundtable-choices { display: grid; gap: 4px; width: min(300px, calc(100vw - 24px)); max-height: min(320px, 45dvh); overflow-y: auto; padding: 6px; border: 1px solid #dfe2f5; border-radius: 12px; background: white; box-shadow: 0 12px 30px #24295c30; }
.roundtable-choices.is-assistant-popup { position: fixed; z-index: var(--layer-assistant-popover); }
.roundtable-dock.is-assistant .roundtable-dock-error { position: absolute; top: calc(100% + 8px); right: 0; z-index: 1; }
.roundtable-choice { display: flex; justify-content: space-between; align-items: center; gap: 8px; min-height: 40px; padding: 7px 9px; border: 0; border-radius: 8px; background: transparent; color: #292e67; text-align: left; cursor: pointer; }
.roundtable-choice:hover { background: #f2f3ff; }
.roundtable-choice span { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.roundtable-choice small { flex: 0 0 auto; color: #68708e; }
.roundtable-load-more { min-height: 40px; border: 0; border-top: 1px solid #eef0fa; background: white; color: #4547ad; font: inherit; font-size: 12px; cursor: pointer; }
.roundtable-load-more:disabled { opacity: 0.65; cursor: wait; }
.roundtable-load-more:focus-visible { outline: 2px solid #5b58e8; outline-offset: -2px; }
.roundtable-dock-error { max-width: 260px; padding: 8px 10px; border-radius: 8px; background: #fff8ed; color: #915018; font-size: 12px; }
.roundtable-dock-error button { margin-left: 7px; padding: 2px 4px; border: 0; background: transparent; color: #5b58e8; cursor: pointer; }
@media (max-width: 920px) {
  .roundtable-fab.is-assistant { position: relative; justify-content: center; width: 40px; min-width: 40px; height: 40px; min-height: 40px; padding: 0; }
  .roundtable-fab.is-assistant .roundtable-label { display: none; }
  .roundtable-fab.is-assistant .roundtable-count { position: absolute; top: -5px; right: -5px; min-width: 17px; height: 17px; padding: 0 3px; border: 1px solid #fff; }
}
</style>
