<template>
  <div v-if="sessions.length || selected || listError" class="roundtable-dock">
    <div v-if="listError" class="roundtable-dock-error" role="status">
      {{ listError }}
      <button type="button" @click="loadSessions">重试</button>
    </div>
    <div v-if="(sessions.length > 1 || nextOffset !== null) && showChoices && !visible" class="roundtable-choices" aria-label="我的圆桌讨论">
      <button
        v-for="session in sessions"
        :key="session.session_id"
        type="button"
        class="roundtable-choice"
        @click="openSession(session.session_id)"
      >
        <span>{{ session.file_name || '未命名文件' }}</span>
        <small>{{ statusLabel(session.status, session.progress?.phase) }}</small>
      </button>
      <button v-if="nextOffset !== null" type="button" class="roundtable-load-more" :disabled="moreLoading" @click="loadMore">
        {{ moreLoading ? '正在加载…' : '加载更多圆桌' }}
      </button>
    </div>
    <button
      v-if="!visible && (sessions.length || selected)"
      class="roundtable-fab"
      type="button"
      aria-label="打开圆桌讨论"
      @click="openFromDock"
    >
      <span aria-hidden="true">🗣️</span>
      <span>圆桌讨论</span>
      <span class="roundtable-count">{{ sessions.length || 1 }}{{ nextOffset !== null ? '+' : '' }}</span>
    </button>
  </div>

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
    @close="visible = false"
    @settled="loadSessions"
  />
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
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
const listError = ref('')
const nextOffset = ref<number | null>(null)
const moreLoading = ref(false)
let authGeneration = 0
let listGeneration = 0
let refreshTimer: ReturnType<typeof setInterval> | null = null

const routeSessionId = computed(() => {
  const raw = route.query.discuss_session
  return typeof raw === 'string' ? raw : Array.isArray(raw) ? raw[0] || '' : ''
})

/** 列表与详情均由服务端按登录身份过滤；迟到响应不得覆盖切换后的账号。 */
async function loadSessions(): Promise<void> {
  const generation = ++listGeneration
  const auth = authGeneration
  const owner = props.userId
  try {
    const page = await listDiscussionSessions()
    if (generation !== listGeneration || auth !== authGeneration || owner !== props.userId) return
    sessions.value = Array.isArray(page?.items) ? page.items : []
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
    nextOffset.value = typeof page.next_offset === 'number' ? page.next_offset : null
    listError.value = ''
  } catch {
    if (generation === listGeneration && auth === authGeneration && owner === props.userId) {
      listError.value = '更多圆桌暂时无法读取'
    }
  } finally {
    moreLoading.value = false
  }
}

async function openSession(sessionId: string): Promise<void> {
  const owner = props.userId
  const generation = authGeneration
  try {
    const detail = await getDiscussionSession(sessionId)
    if (owner !== props.userId || generation !== authGeneration) return
    if (!detail || detail.session_id !== sessionId) return
    selected.value = { ...detail, agents: Array.isArray(detail.agents) ? detail.agents : [] }
    visible.value = true
    showChoices.value = false
    listError.value = ''
  } catch {
    if (owner !== props.userId || generation !== authGeneration) return
    listError.value = '圆桌会话已失效或当前账号无权查看'
  }
}

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
}

function statusLabel(status: string, progressPhase?: string): string {
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

watch(() => props.userId, () => {
  authGeneration++
  listGeneration++
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
  document.addEventListener('visibilitychange', onVisibilityChange)
  window.addEventListener('prism:open-roundtable', onOpenEvent)
  window.addEventListener('prism:roundtable-list-changed', onRoundtableListChanged)
  // 后台工具完成事件可能发生在异步宿主挂载前；可见页低频对账兜底。
  refreshTimer = setInterval(() => {
    if (document.visibilityState === 'visible') void loadSessions()
  }, 30_000)
})
onBeforeUnmount(() => {
  authGeneration++
  listGeneration++
  document.removeEventListener('visibilitychange', onVisibilityChange)
  window.removeEventListener('prism:open-roundtable', onOpenEvent)
  window.removeEventListener('prism:roundtable-list-changed', onRoundtableListChanged)
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<style scoped>
.roundtable-dock { position: fixed; right: 24px; bottom: 100px; z-index: 2990; display: grid; justify-items: end; gap: 8px; max-width: min(320px, calc(100vw - 24px)); }
.roundtable-fab { display: inline-flex; align-items: center; gap: 8px; min-height: 44px; padding: 8px 12px; border: 1px solid #d9ddfa; border-radius: 999px; background: #fff; color: #292e67; box-shadow: 0 8px 28px #24295c33; font: inherit; font-size: 13px; font-weight: 650; cursor: pointer; }
.roundtable-fab:focus-visible, .roundtable-choice:focus-visible { outline: 2px solid #5b58e8; outline-offset: 2px; }
.roundtable-count { display: inline-grid; place-items: center; min-width: 22px; height: 22px; padding: 0 4px; border-radius: 999px; background: #eef0ff; font-size: 11px; }
.roundtable-choices { display: grid; gap: 4px; width: min(300px, calc(100vw - 24px)); max-height: min(320px, 45dvh); overflow-y: auto; padding: 6px; border: 1px solid #dfe2f5; border-radius: 12px; background: white; box-shadow: 0 12px 30px #24295c30; }
.roundtable-choice { display: flex; justify-content: space-between; align-items: center; gap: 8px; min-height: 40px; padding: 7px 9px; border: 0; border-radius: 8px; background: transparent; color: #292e67; text-align: left; cursor: pointer; }
.roundtable-choice:hover { background: #f2f3ff; }
.roundtable-choice span { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.roundtable-choice small { flex: 0 0 auto; color: #68708e; }
.roundtable-load-more { min-height: 40px; border: 0; border-top: 1px solid #eef0fa; background: white; color: #4547ad; font: inherit; font-size: 12px; cursor: pointer; }
.roundtable-load-more:disabled { opacity: 0.65; cursor: wait; }
.roundtable-load-more:focus-visible { outline: 2px solid #5b58e8; outline-offset: -2px; }
.roundtable-dock-error { max-width: 260px; padding: 8px 10px; border-radius: 8px; background: #fff8ed; color: #915018; font-size: 12px; }
.roundtable-dock-error button { margin-left: 7px; padding: 2px 4px; border: 0; background: transparent; color: #5b58e8; cursor: pointer; }
@media (max-width: 520px) { .roundtable-dock { right: 12px; bottom: calc(82px + env(safe-area-inset-bottom)); } }
</style>
