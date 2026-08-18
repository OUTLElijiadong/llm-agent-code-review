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
  setAgentChatSessionPinned,
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
  /** 请求父组件把会话归档到服务端,再配合本地移除。 */
  archive: [sessionId: string]
}>()

const sessions = ref<AgentChatSessionMeta[]>([])
const activeId = ref('')
const menuFor = ref('')
const searchQuery = ref('')
const confirmingDeleteId = ref('')
const archivingId = ref('')
const busyIds = ref<Set<string>>(new Set())
const panelRef = ref<HTMLElement | null>(null)
const discoveryLoading = ref(false)
/** 服务端权威运行状态(仅 session 项);数据库为忙碌状态的唯一事实源。 */
const remoteRunState = ref<Map<string, string>>(new Map())
const discoveryLoadedOnce = ref(false)
let discoveryTimer: number | undefined
let pendingHeartbeatId = ''

const currentTitle = computed(() => (
  sessions.value.find((item) => item.id === activeId.value)?.title ?? '对话'
))

/** 搜索过滤 + 置顶优先;底层的 sessions 顺序仍由服务端合并结果决定。 */
const displaySessions = computed<AgentChatSessionMeta[]>(() => {
  const query = searchQuery.value.trim().toLowerCase()
  const base = query
    ? sessions.value.filter((item) => item.title.toLowerCase().includes(query))
    : [...sessions.value]
  return base.slice().sort((left, right) => Number(Boolean(right.pinned)) - Number(Boolean(left.pinned)))
})

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

/** 外部主动聚焦某会话(如后台 mesh 消息处理完成):仅当目标在列表且非当前忙碌时切换。 */
function focusSession(sessionId: string): void {
  if (!sessionId || sessionId === activeId.value) return
  if (!sessions.value.some((item) => item.id === sessionId)) return
  // 当前会话忙碌(运行/等待审批)时不切走,保留用户上下文。
  const current = sessions.value.find((item) => item.id === activeId.value)
  if (current && inferBusy(current)) return
  select(sessionId)
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

function togglePin(sessionId: string): void {
  const target = sessions.value.find((item) => item.id === sessionId)
  if (!target) return
  setAgentChatSessionPinned(props.storageKey, sessionId, target.pinned !== true)
  sessions.value = loadAgentChatSessions(props.storageKey, props.legacyKey, props.idPrefix)
  notify()
}

function requestDelete(sessionId: string): void {
  const target = sessions.value.find((item) => item.id === sessionId)
  if (!target || inferBusy(target)) return
  confirmingDeleteId.value = sessionId
}

function cancelDelete(): void {
  confirmingDeleteId.value = ''
}

function confirmDelete(sessionId: string): void {
  confirmingDeleteId.value = ''
  archivingId.value = sessionId
  // 通知父组件执行服务端归档;成功后父组件调用 removeSession 完成本地移除。
  emit('archive', sessionId)
}

/** 服务端归档成功后由父组件调用,完成本地移除与切换。 */
function removeSession(sessionId: string): void {
  archivingId.value = ''
  dropSession(sessionId)
}

/** 服务端归档失败时由父组件调用,清空归档中状态并保留会话。 */
function restoreSessionAfterArchiveFailure(): void {
  archivingId.value = ''
}

async function ensureFreshOnOpen(): Promise<void> {
  // 首次打开前先拉一次服务端权威状态,避免用陈旧本地快照把已完成会话当忙碌。
  // 必须 await 完成后再做下面的 pristine/新建判断,否则在 discovery 未落库时
  // 会把「服务端 running 但本地无快照」的会话误判为空,进而误新建顶掉它。
  if (props.discoverRemote && !discoveryLoadedOnce.value) {
    await refreshFromAgentMesh()
  }
  // discovery 完成后,会话列表与权威运行状态已就位;此前的任何 select/新建都基于
  // 不完整信息,这里以最新 sessions 重新评估,确保忙碌会话不会被误切/覆盖。
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
  //    pristine 判断只看本地快照,对「无本地快照但服务端 running」的会话会误判为空,
  //    因此叠加 inferBusy(含服务端权威状态)兜底,忙碌会话绝不当作空会话被跳过/顶掉。
  if (current && !inferBusy(current) && isPristineAgentChatSession(current.id, welcomeText, current.title)) return
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
  confirmingDeleteId.value = ''
}

function handleOutsideClick(event: MouseEvent): void {
  if (!menuFor.value) return
  if (panelRef.value && !panelRef.value.contains(event.target as Node)) closeMenu()
}

/** 根据服务端权威状态与本地快照推断会话是否忙碌。 */
function inferBusy(meta: AgentChatSessionMeta): boolean {
  if (busyIds.value.has(meta.id)) return true
  // 服务端 agent_response_run 是权威事实源:后台已完成时,即使本地快照陈旧为
  // running/waiting,也不能再把会话当忙碌从而阻止“打开即新对话”。
  const remote = remoteRunState.value.get(meta.id)
  if (remote !== undefined) return isAgentResponseSessionOccupied(remote)
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
  // 首次挂载也要执行一次“打开即新对话”策略，避免父组件 visible watcher 在
  // 子组件尚未就绪时错过 ensureFreshOnOpen，导致页面加载后第一次点开仍复用旧会话。
  void ensureFreshOnOpen()
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
        activeRunId: item.active_run_id,
        activeRunStatus: item.active_run_status,
      }))
    const localBeforeMerge = loadAgentChatSessions(props.storageKey, props.legacyKey, props.idPrefix)
    // 服务端发现前,当前会话也必须保留,否则首次心跳尚未落库时会被误删并切走,
    // 正在运行/等待审批的会话尤其不能因为一次空发现而丢失上下文。
    const preserveIds = new Set<string>()
    if (pendingHeartbeatId) preserveIds.add(pendingHeartbeatId)
    if (activeId.value) preserveIds.add(activeId.value)
    let merged = mergeAgentChatSessions(
      localBeforeMerge,
      discovered,
      surface,
      preserveIds,
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
    const nextRunState = new Map<string, string>()
    for (const item of discovered) {
      // 服务端已发现该会话却没有运行记录时，权威状态等价于“已完成/不忙”，
      // 必须覆盖陈旧本地快照，否则无 run 行的历史会话仍会被误判为忙碌。
      const status = typeof item.activeRunStatus === 'string' && item.activeRunStatus.length > 0
        ? item.activeRunStatus
        : 'completed'
      nextRunState.set(item.id, status)
    }
    remoteRunState.value = nextRunState
    discoveryLoadedOnce.value = true
    if (!merged.some((item) => item.id === activeId.value)) {
      const busy = busyIds.value.has(activeId.value)
      if (busy) {
        // 当前会话正在运行/等待:不自动切走,保留其上下文直到服务端权威状态收敛。
        const activeMeta = sessions.value.find((item) => item.id === activeId.value)
        if (activeMeta && !merged.some((item) => item.id === activeMeta.id)) merged.unshift(activeMeta)
      } else {
        activeId.value = merged[0].id
        if (activeId.value !== previousActiveId) emit('select', activeId.value)
      }
    } else {
      // 自动聚焦「最新活跃对话」:仅当前停留在「确知的空会话」时让位,
      // 有内容/忙碌/非占位标题的对话绝不切走(保留用户上下文)。
      // 注意:此处 remoteRunState 已在上方赋值完成,inferBusy 用的是权威状态。
      const currentMeta = sessions.value.find((item) => item.id === activeId.value)
      const currentIdle = currentMeta
        && !inferBusy(currentMeta)
        && isPristineAgentChatSession(currentMeta.id, props.welcomeText ?? '', currentMeta.title)
      if (currentIdle) {
        const liveliest = discovered
          .filter((item) => item.id !== activeId.value && merged.some((m) => m.id === item.id))
          .sort((a, b) => Date.parse(b.lastSeenAt || '') - Date.parse(a.lastSeenAt || ''))[0]
        if (liveliest) {
          // 该会话刚活跃过(近 30s),或正有任务在跑——值得把用户带过去。
          const recent = Date.now() - Date.parse(liveliest.lastSeenAt || '') < 30_000
          const occupied = isAgentResponseSessionOccupied(liveliest.activeRunStatus)
          if (recent || occupied) {
            activeId.value = liveliest.id
            if (activeId.value !== previousActiveId) emit('select', activeId.value)
          }
        }
      }
    }
    notify()
  } catch {
    // 发现接口短暂不可用时保留本地列表，下一次打开或轮询继续收敛。
  } finally {
    discoveryLoading.value = false
  }
}

defineExpose({ setBusy, renameActive, createSession, ensureFreshOnOpen, reload, refreshFromAgentMesh, removeSession, restoreSessionAfterArchiveFailure, focusSession })
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
        <input
          v-model="searchQuery"
          class="session-search"
          type="search"
          placeholder="搜索对话"
          aria-label="搜索对话"
        />
        <div v-if="!displaySessions.length" class="session-empty">没有匹配的对话</div>
        <div
          v-for="item in displaySessions"
          :key="item.id"
          class="session-entry"
        >
          <button
            class="session-item"
            :class="{ 'is-active': item.id === activeId }"
            type="button"
            role="menuitem"
            @click.stop="select(item.id)"
          >
            <span
              v-if="inferBusy(item)"
              class="session-busy-dot"
              title="正在后台运行，暂不可删除"
              aria-label="正在后台运行"
            />
            <span class="session-item-name">{{ item.title }}</span>
            <span
              class="session-pin"
              :class="{ 'is-pinned': item.pinned === true }"
              role="button"
              tabindex="-1"
              :aria-label="item.pinned ? `取消置顶 ${item.title}` : `置顶 ${item.title}`"
              :title="item.pinned ? '取消置顶' : '置顶'"
              @click.stop="togglePin(item.id)"
            >{{ item.pinned ? '📍' : '📌' }}</span>
            <span
              v-if="sessions.length > 1 && !inferBusy(item) && archivingId !== item.id"
              class="session-delete"
              role="button"
              tabindex="-1"
              :aria-label="`删除对话 ${item.title}`"
              :title="`删除对话 ${item.title}`"
              @click.stop="requestDelete(item.id)"
            >×</span>
            <span
              v-else-if="sessions.length > 1 && archivingId === item.id"
              class="session-archiving"
              title="正在归档"
              aria-label="正在归档"
            >…</span>
            <span
              v-else-if="sessions.length > 1"
              class="session-lock"
              title="运行中，不可删除"
              aria-label="运行中，不可删除"
            >🔒</span>
          </button>
          <div
            v-if="confirmingDeleteId === item.id"
            class="session-confirm"
            role="alertdialog"
            aria-label="确认删除对话"
            @click.stop
          >
            <span class="session-confirm-text">确认归档该对话？归档后将从会话列表移除。</span>
            <button class="session-confirm-yes" type="button" @click.stop="confirmDelete(item.id)">删除</button>
            <button class="session-confirm-no" type="button" @click.stop="cancelDelete()">取消</button>
          </div>
        </div>
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

.session-search {
  width: 100%;
  box-sizing: border-box;
  margin: 0 0 6px;
  padding: 6px 9px;
  border: 1px solid var(--color-border-light, #e5e6eb);
  border-radius: 7px;
  background: var(--gray-50, #f7f8fa);
  color: var(--color-text-primary, #1f2329);
  font-size: 12.5px;
  outline: none;
}

.session-search:focus {
  border-color: var(--brand-400, #7a77ee);
  background: #fff;
}

.session-empty {
  padding: 14px 8px;
  color: var(--color-text-placeholder, #a8abb2);
  font-size: 12.5px;
  text-align: center;
}

.session-pin {
  flex-shrink: 0;
  display: grid;
  place-items: center;
  width: 18px;
  height: 18px;
  border-radius: 5px;
  font-size: 11px;
  line-height: 1;
  opacity: 0.35;
}

.session-pin:hover {
  background: rgba(122, 119, 238, 0.12);
  opacity: 1;
}

.session-pin.is-pinned {
  opacity: 1;
}

.session-lock {
  flex-shrink: 0;
  display: grid;
  place-items: center;
  width: 18px;
  height: 18px;
  font-size: 10px;
  line-height: 1;
  opacity: 0.65;
}

.session-archiving {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  color: var(--brand-500, #5b58e8);
  font-size: 12px;
  animation: session-busy-pulse 0.9s ease-in-out infinite;
}

.session-confirm {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-radius: 7px;
  background: rgba(220, 73, 97, 0.08);
}

.session-confirm-text {
  flex: 1;
  font-size: 11.5px;
  color: #a33c4e;
}

.session-confirm-yes,
.session-confirm-no {
  padding: 3px 8px;
  border-radius: 6px;
  font-size: 11.5px;
  cursor: pointer;
}

.session-confirm-yes {
  border: 1px solid #dc4961;
  background: #dc4961;
  color: #fff;
}

.session-confirm-no {
  border: 1px solid var(--color-border-light, #d8dade);
  background: #fff;
  color: var(--color-text-secondary, #5b616b);
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
