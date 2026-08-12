<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import {
  createAgentChatSession,
  findPristineAgentChatSession,
  isPristineAgentChatSession,
  loadAgentChatSessions,
  loadAgentChatSnapshot,
  mergeAgentChatSessions,
  removeAgentChatSession,
  renameAgentChatSession,
  saveAgentChatSessions,
  type AgentChatSessionMeta,
  type DiscoveredAgentChatSession,
} from '@/utils/agentChatSessions'
import { listAgentMeshAgents } from '@/api/agentMesh'
import { isAgentResponseSessionOccupied } from '@/utils/agentResponseSession'

interface Props {
  /** localStorage 命名空间:user / admin 各自独立 */
  storageKey: string
  /** 旧版单会话 localStorage 键,用于迁移 */
  legacyKey: string
  /** 新会话 id 前缀 */
  idPrefix: string
  /** 本地欢迎语不计入有效消息,用于判断空会话能否复用 */
  welcomeText?: string
  /** 启用 Agent Mesh 服务端会话发现；user/admin 宿主显式传入。 */
  discoverRemote?: boolean
}

const props = defineProps<Props>()
const emit = defineEmits<{
  select: [sessionId: string]
  'sessions-changed': [metas: AgentChatSessionMeta[]]
}>()

const sessions = ref<AgentChatSessionMeta[]>([])
const activeId = ref('')
const menuFor = ref('')
const busyIds = ref<Set<string>>(new Set())
const panelRef = ref<HTMLElement | null>(null)
const discoveryLoading = ref(false)
let discoveryTimer: number | undefined
let pendingHeartbeatId = ''

const currentTitle = computed(() => (
  sessions.value.find((item) => item.id === activeId.value)?.title ?? '对话'
))

/** 由父组件在恢复/轮询/流事件后同步各会话占用状态。 */
function setBusy(sessionId: string, busy: boolean): void {
  const next = new Set(busyIds.value)
  if (busy) next.add(sessionId)
  else next.delete(sessionId)
  busyIds.value = next
}

function notify(): void {
  emit('sessions-changed', sessions.value)
}

function select(sessionId: string): void {
  if (sessionId === activeId.value) {
    menuFor.value = ''
    return
  }
  activeId.value = sessionId
  menuFor.value = ''
  emit('select', sessionId)
}

function createSession(): void {
  const meta = createAgentChatSession(props.storageKey, props.idPrefix)
  pendingHeartbeatId = meta.id
  sessions.value = loadAgentChatSessions(props.storageKey, props.legacyKey, props.idPrefix)
  notify()
  select(meta.id)
}

function dropSession(sessionId: string): void {
  if (inferBusy(sessions.value.find((item) => item.id === sessionId) ?? { id: sessionId, title: '', createdAt: 0 })) return
  if (pendingHeartbeatId === sessionId) pendingHeartbeatId = ''
  const wasActive = sessionId === activeId.value
  removeAgentChatSession(props.storageKey, sessionId)
  sessions.value = loadAgentChatSessions(props.storageKey, props.legacyKey, props.idPrefix)
  if (!sessions.value.length) {
    const meta = createAgentChatSession(props.storageKey, props.idPrefix)
    sessions.value = [meta]
  }
  notify()
  if (wasActive) select(sessions.value[0].id)
}

function ensureFreshOnOpen(): void {
  const welcomeText = props.welcomeText ?? ''
  const current = sessions.value.find((item) => item.id === activeId.value)
  // 1) 任一历史会话未完成(运行中/等待审批/等待输入) → 优先跳回该会话,
  //    保留正在执行任务的上下文;当前就是这个会话时保持不动。
  const busySession = sessions.value.find((item) => inferBusy(item))
  if (busySession) {
    if (busySession.id !== activeId.value) select(busySession.id)
    return
  }
  // 2) 当前就是空的新对话(无输入输出) → 保留它,不重复创建空白条目。
  if (current && isPristineAgentChatSession(current.id, welcomeText)) return
  // 3) 历史对话均已完成后:复用既有空对话。
  const reusable = findPristineAgentChatSession(sessions.value, welcomeText, busyIds.value)
  if (reusable) {
    select(reusable.id)
    return
  }
  // 4) 全部为已完成对话且无空会话 → 新建空对话。
  createSession()
}

function toggleMenu(): void {
  menuFor.value = menuFor.value ? '' : 'open'
}

function closeMenu(): void {
  menuFor.value = ''
}

function handleOutsideClick(event: MouseEvent): void {
  if (!menuFor.value) return
  if (panelRef.value && !panelRef.value.contains(event.target as Node)) closeMenu()
}

/** 根据服务端状态与本地快照推断会话是否忙碌。 */
function inferBusy(meta: AgentChatSessionMeta): boolean {
  if (busyIds.value.has(meta.id)) return true
  const snapshot = loadAgentChatSnapshot(meta.id)
  return isAgentResponseSessionOccupied(snapshot?.runStatus)
}

onMounted(() => {
  sessions.value = loadAgentChatSessions(props.storageKey, props.legacyKey, props.idPrefix)
  if (!sessions.value.length) {
    const meta = createAgentChatSession(props.storageKey, props.idPrefix)
    sessions.value = [meta]
    pendingHeartbeatId = meta.id
  }
  activeId.value = sessions.value[0].id
  notify()
  emit('select', activeId.value)
  if (props.discoverRemote) {
    void refreshFromAgentMesh()
    discoveryTimer = window.setInterval(() => void refreshFromAgentMesh(), 10_000)
  }
  document.addEventListener('click', handleOutsideClick)
})

onBeforeUnmount(() => {
  if (discoveryTimer !== undefined) window.clearInterval(discoveryTimer)
  document.removeEventListener('click', handleOutsideClick)
})

function renameActive(title: string): void {
  if (!activeId.value) return
  renameAgentChatSession(props.storageKey, activeId.value, title)
  sessions.value = loadAgentChatSessions(props.storageKey, props.legacyKey, props.idPrefix)
  notify()
}

/** 重新从本地存储加载会话列表(自动命名等外部改动后刷新标题)。 */
function reload(): void {
  sessions.value = loadAgentChatSessions(props.storageKey, props.legacyKey, props.idPrefix)
  notify()
}

/**
 * 以 Agent Mesh 服务端会话目录为事实源，合并当前 surface 的本地标题和最近顺序。
 * 未被服务端发现的本地条目不会继续出现在切换器中，避免账户或 user/admin 数据串线。
 */
async function refreshFromAgentMesh(): Promise<void> {
  if (discoveryLoading.value) return
  discoveryLoading.value = true
  const surface = props.storageKey === 'admin' ? 'admin' : 'user'
  const previousActiveId = activeId.value
  try {
    const discovery = await listAgentMeshAgents()
    const discovered: DiscoveredAgentChatSession[] = discovery.items
      .filter((item) => item.kind === 'session' && item.surface === surface && item.session_id)
      .map((item) => ({
        id: item.session_id,
        title: item.name,
        surface,
        kind: 'session' as const,
        lastSeenAt: item.last_seen_at,
      }))
    let merged = mergeAgentChatSessions(
      loadAgentChatSessions(props.storageKey, props.legacyKey, props.idPrefix),
      discovered,
      surface,
      new Set(pendingHeartbeatId ? [pendingHeartbeatId] : []),
    )
    if (pendingHeartbeatId && discovered.some((item) => item.id === pendingHeartbeatId)) pendingHeartbeatId = ''
    if (!merged.length) {
      // 服务端还未收到当前窗口的首次 heartbeat，建立一个干净的当前端会话。
      const current = createAgentChatSession(props.storageKey, props.idPrefix)
      pendingHeartbeatId = current.id
      merged = [current]
    }
    saveAgentChatSessions(props.storageKey, merged)
    sessions.value = merged
    if (!merged.some((item) => item.id === activeId.value)) {
      activeId.value = merged[0].id
      if (activeId.value !== previousActiveId) emit('select', activeId.value)
    }
    notify()
  } catch {
    // 发现接口短暂不可用时保留本地列表，下一次打开或轮询继续收敛。
  } finally {
    discoveryLoading.value = false
  }
}

defineExpose({ setBusy, renameActive, createSession, ensureFreshOnOpen, reload, refreshFromAgentMesh })
</script>

<template>
  <div ref="panelRef" class="session-switch">
    <button
      class="session-current"
      type="button"
      :title="`当前对话:${currentTitle},点击切换对话`"
      @click.stop="toggleMenu"
    >
      <span class="session-name">{{ currentTitle }}</span>
      <svg class="session-caret" :class="{ 'is-open': !!menuFor }" width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
        <path d="M2 3.5 L5 6.5 L8 3.5" stroke="currentColor" stroke-width="1.4" fill="none" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
    </button>
    <button
      class="session-new"
      type="button"
      title="新建对话(正在运行的对话会在后台继续)"
      aria-label="新建对话"
      @click.stop="createSession"
    >
      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
        <path d="M6 1.5 V10.5 M1.5 6 H10.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
      </svg>
    </button>

    <Transition name="session-pop">
      <div v-if="menuFor" class="session-menu" role="menu">
        <div class="session-menu-title">我的对话</div>
        <button
          v-for="item in sessions"
          :key="item.id"
          class="session-item"
          :class="{ 'is-active': item.id === activeId }"
          type="button"
          role="menuitem"
          @click.stop="select(item.id)"
        >
          <span v-if="inferBusy(item)" class="session-busy-dot" title="正在后台运行" aria-label="正在后台运行"></span>
          <span class="session-item-name">{{ item.title }}</span>
          <span
            v-if="sessions.length > 1"
            class="session-delete"
            role="button"
            tabindex="-1"
            :aria-label="`删除对话 ${item.title}`"
            :title="`删除对话 ${item.title}`"
            @click.stop="dropSession(item.id)"
          >×</span>
        </button>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.session-switch {
  position: relative;
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}

.session-current {
  display: flex;
  align-items: center;
  gap: 5px;
  max-width: 132px;
  padding: 4px 8px;
  border: 1px solid var(--color-border-light, #e5e6eb);
  border-radius: 7px;
  background: var(--gray-50, #f7f8fa);
  color: var(--color-text-secondary, #5b616b);
  font-size: 12px;
  cursor: pointer;
  transition: border-color 0.15s ease;
}

.session-current:hover {
  border-color: var(--brand-400, #7a77ee);
  color: var(--brand-600, #4a47d1);
}

.session-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-caret {
  flex-shrink: 0;
  transition: transform 0.15s ease;
}

.session-caret.is-open {
  transform: rotate(180deg);
}

.session-new {
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  border: 1px dashed var(--color-border-light, #d8dade);
  border-radius: 7px;
  background: transparent;
  color: var(--color-text-secondary, #8f959e);
  cursor: pointer;
  transition: all 0.15s ease;
}

.session-new:hover {
  border-color: var(--brand-500, #5b58e8);
  color: var(--brand-500, #5b58e8);
}

.session-menu {
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  z-index: 20;
  width: 220px;
  max-height: 260px;
  overflow-y: auto;
  padding: 6px;
  border: 1px solid var(--color-border-light, #e5e6eb);
  border-radius: 10px;
  background: #fff;
  box-shadow: 0 10px 30px rgba(15, 18, 34, 0.14);
}

.session-menu-title {
  padding: 4px 8px 6px;
  font-size: 10.5px;
  color: var(--color-text-placeholder, #a8abb2);
}

.session-item {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 7px 8px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--color-text-primary, #1f2329);
  font-size: 12.5px;
  text-align: left;
  cursor: pointer;
}

.session-item:hover {
  background: var(--gray-50, #f4f5f8);
}

.session-item.is-active {
  background: var(--brand-50, #eef0ff);
  color: var(--brand-600, #4a47d1);
  font-weight: 600;
}

.session-item-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-busy-dot {
  flex-shrink: 0;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #3dbcd9;
  animation: session-busy-pulse 1.1s ease-in-out infinite;
}

.session-delete {
  flex-shrink: 0;
  display: grid;
  place-items: center;
  width: 18px;
  height: 18px;
  border-radius: 5px;
  color: var(--color-text-placeholder, #a8abb2);
  font-size: 13px;
  line-height: 1;
}

.session-delete:hover {
  background: rgba(220, 73, 97, 0.12);
  color: #dc4961;
}

@keyframes session-busy-pulse {
  0%, 100% { opacity: 0.35; transform: scale(0.85); }
  50% { opacity: 1; transform: scale(1.1); }
}

.session-pop-enter-active,
.session-pop-leave-active {
  transition: opacity 0.16s ease, transform 0.16s ease;
}

.session-pop-enter-from,
.session-pop-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>
