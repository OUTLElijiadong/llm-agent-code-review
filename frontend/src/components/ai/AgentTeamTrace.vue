<script setup lang="ts">
import { computed, getCurrentInstance, inject, ref, watch } from 'vue'

import { listAgentTeamMessages } from '@/api/agentTeams'
import type {
  AgentTeamDetail,
  AgentTeamEvent,
  AgentTeamMember,
  AgentTeamMessage,
  AgentTeamSummary,
  AgentTeamTask,
} from '@/api/agentTeams'
import AgentMemberWorkCard from '@/components/ai/AgentMemberWorkCard.vue'
import { formatDateTime, parseUtcTimestamp } from '@/utils/format'
import { messageActivityExpandedKey } from '@/components/ai/agentActivityDisclosure'

const props = withDefaults(defineProps<{
  team: AgentTeamDetail | AgentTeamSummary | null
  loading?: boolean
  error?: string
}>(), {
  loading: false,
  error: '',
})

/** 请求在独立悬浮窗(AgentTeamWindow)中查看该团队的完整工作内容。 */
const emit = defineEmits<{ 'open-detail': [team: AgentTeamDetail | AgentTeamSummary] }>()

function openDetail(): void {
  if (props.team) emit('open-detail', props.team)
}

const expanded = ref(false)
const parentExpanded = inject(messageActivityExpandedKey, null)
const embedded = computed(() => parentExpanded !== null)
const detailsExpanded = computed(() => parentExpanded?.value ?? expanded.value)
const tracePanelId = `agent-team-trace-panel-${getCurrentInstance()?.uid ?? 'instance'}`
const selectedRootKey = ref<string | null>(null)
const TRACE_PAGE_SIZE = 12
const visibleTaskCount = ref(TRACE_PAGE_SIZE)
const visibleEventCount = ref(TRACE_PAGE_SIZE)
const visibleMessageCount = ref(TRACE_PAGE_SIZE)
const olderMessages = ref<AgentTeamMessage[]>([])
const messageHistoryLoading = ref(false)
const messageHistoryError = ref('')
const messageHasMore = ref(false)
const messageBeforeId = ref<number | null>(null)
const messageTotal = ref(0)

const teamDetail = computed(() => props.team as AgentTeamDetail | null)
const members = computed<AgentTeamMember[]>(() => teamDetail.value?.members ?? [])
const tasks = computed<AgentTeamTask[]>(() => teamDetail.value?.tasks ?? [])
const events = computed<AgentTeamEvent[]>(() => teamDetail.value?.events ?? [])
const messages = computed<AgentTeamMessage[]>(() => {
  const merged = [...olderMessages.value, ...(teamDetail.value?.messages ?? [])]
  const unique = new Map<string, AgentTeamMessage>()
  for (const message of merged) unique.set(message.message_id, message)
  return [...unique.values()].sort((left, right) => {
    if (left.ledger_id !== undefined && right.ledger_id !== undefined) return left.ledger_id - right.ledger_id
    return 0
  })
})
const visibleTasks = computed(() => tasks.value.slice(0, visibleTaskCount.value))
const visibleEvents = computed(() => events.value.slice(-visibleEventCount.value))
const visibleMessages = computed(() => messages.value.slice(-visibleMessageCount.value))
const counts = computed(() => props.team?.counts ?? {
  total: tasks.value.length,
  completed: tasks.value.filter((task) => task.status === 'completed').length,
  running: tasks.value.filter((task) => task.status === 'running').length,
  queued: tasks.value.filter((task) => ['queued', 'waiting_dependency'].includes(task.status)).length,
  failed: tasks.value.filter((task) => ['failed', 'dead_letter', 'expired'].includes(task.status)).length,
  blocked: tasks.value.filter((task) => task.status === 'blocked').length,
})
const progressPercent = computed(() => {
  if (counts.value.total <= 0) return null
  return Math.max(0, Math.min(100, Math.round((counts.value.completed / counts.value.total) * 100)))
})
const problemCount = computed(() => counts.value.failed + counts.value.blocked)

interface DependencyGraphNode {
  task: AgentTeamTask
  x: number
  y: number
  isRoot: boolean
}

interface DependencyGraphEdge {
  sourceKey: string
  targetKey: string
  path: string
}

function graphPoint(index: number): { x: number; y: number } {
  return { x: 140 + (index % 3) * 360, y: 52 + Math.floor(index / 3) * 112 }
}

function graphPath(source: DependencyGraphNode, target: DependencyGraphNode): string {
  const direction = source.x <= target.x ? 1 : -1
  const sourceX = source.x + direction * 102
  const targetX = target.x - direction * 102
  const bend = Math.max(30, Math.abs(targetX - sourceX) * .42)
  return `M ${sourceX} ${source.y} C ${sourceX + direction * bend} ${source.y}, ${targetX - direction * bend} ${target.y}, ${targetX} ${target.y}`
}

const dependencyGraph = computed(() => {
  const allTasks = tasks.value as Array<AgentTeamTask & { depends_on?: unknown }>
  const rawTasks = visibleTasks.value as Array<AgentTeamTask & { depends_on?: unknown }>
  if (!rawTasks.length || allTasks.some((task) => (
    typeof task.task_key !== 'string'
    || !task.task_key.trim()
    || !Array.isArray(task.depends_on)
    || task.depends_on.some((dependency) => typeof dependency !== 'string' || !dependency.trim())
  ))) return null

  const taskByKey = new Map(allTasks.map((task) => [task.task_key, task]))
  if (taskByKey.size !== allTasks.length) return null
  if (allTasks.some((task) => (task.depends_on as string[]).some((dependency) => !taskByKey.has(dependency)))) return null
  const visibleKeys = new Set(rawTasks.map((task) => task.task_key))
  if (rawTasks.some((task) => (task.depends_on as string[]).some((dependency) => !visibleKeys.has(dependency)))) return null

  const nodes: DependencyGraphNode[] = rawTasks.map((task, index) => ({
    task,
    ...graphPoint(index),
    isRoot: (task.depends_on as string[]).length === 0,
  }))
  const nodeByKey = new Map(nodes.map((node) => [node.task.task_key, node]))
  const edges: DependencyGraphEdge[] = rawTasks.flatMap((task) => (task.depends_on as string[]).map((sourceKey) => {
    const source = nodeByKey.get(sourceKey)!
    const target = nodeByKey.get(task.task_key)!
    return { sourceKey, targetKey: task.task_key, path: graphPath(source, target) }
  }))
  const roots = nodes.filter((node) => node.isRoot)
  const height = Math.max(120, Math.ceil(nodes.length / 3) * 112 + 24)
  const labelText = `任务依赖图，${nodes.length} 个任务节点，${edges.length} 条由 depends_on 提供的依赖边。根节点：${roots.length ? roots.map((node) => node.task.title).join('、') : '无'}`
  return { nodes, edges, roots, height, labelText }
})

const selectedRoot = computed(() => dependencyGraph.value?.roots.find((node) => node.task.task_key === selectedRootKey.value) ?? null)
const tracedTaskKeys = computed(() => {
  const graph = dependencyGraph.value
  if (!graph || !selectedRoot.value) return new Set<string>()

  const reachable = new Set<string>([selectedRoot.value.task.task_key])
  const pending = [selectedRoot.value.task.task_key]
  while (pending.length) {
    const sourceKey = pending.shift()!
    for (const edge of graph.edges) {
      if (edge.sourceKey !== sourceKey || reachable.has(edge.targetKey)) continue
      reachable.add(edge.targetKey)
      pending.push(edge.targetKey)
    }
  }
  return reachable
})

function traceFromRoot(taskKey: string): void {
  selectedRootKey.value = selectedRootKey.value === taskKey ? null : taskKey
}

const dependencyMarkerId = computed(() => `agent-team-dependency-arrow-${props.team?.team_id ?? 'unknown'}`)

const STATUS_LABELS: Record<string, string> = {
  draft: '草稿', queued: '排队中', running: '运行中', verifying: '验证中',
  completed: '已完成', failed: '失败', cancelled: '已取消', expired: '已过期',
  created: '已创建', reclaimed: '已回收', waiting_dependency: '等待依赖',
  blocked: '已阻塞', dead_letter: '死信',
}

function label(status: string): string {
  return STATUS_LABELS[status] ?? status
}

function statusClass(status: string): string {
  return `is-${status.replace(/[^a-z0-9_-]/gi, '-')}`
}

function toggle(): void {
  expanded.value = !expanded.value
}

async function showMore(kind: 'task' | 'event' | 'message'): Promise<void> {
  if (kind === 'task') visibleTaskCount.value += TRACE_PAGE_SIZE
  if (kind === 'event') visibleEventCount.value += TRACE_PAGE_SIZE
  if (kind !== 'message') return
  const nextVisibleCount = visibleMessageCount.value + TRACE_PAGE_SIZE
  if (
    nextVisibleCount > messages.value.length
    && messageHasMore.value
    && messageBeforeId.value
    && props.team?.team_id
    && !messageHistoryLoading.value
  ) {
    messageHistoryLoading.value = true
    messageHistoryError.value = ''
    try {
      const page = await listAgentTeamMessages(props.team.team_id, messageBeforeId.value)
      olderMessages.value = [...page.items, ...olderMessages.value]
      messageHasMore.value = page.has_more
      messageBeforeId.value = page.next_before_id ?? null
      messageTotal.value = page.total
    } catch {
      messageHistoryError.value = '早期消息加载失败，请重试'
    } finally {
      messageHistoryLoading.value = false
    }
  }
  visibleMessageCount.value = nextVisibleCount
}

function collapseList(kind: 'task' | 'event' | 'message'): void {
  if (kind === 'task') visibleTaskCount.value = TRACE_PAGE_SIZE
  if (kind === 'event') visibleEventCount.value = TRACE_PAGE_SIZE
  if (kind === 'message') visibleMessageCount.value = TRACE_PAGE_SIZE
}

watch(
  () => props.team?.team_id,
  () => {
    selectedRootKey.value = null
    visibleTaskCount.value = TRACE_PAGE_SIZE
    visibleEventCount.value = TRACE_PAGE_SIZE
    visibleMessageCount.value = TRACE_PAGE_SIZE
    olderMessages.value = []
    messageHistoryError.value = ''
    messageHasMore.value = Boolean(teamDetail.value?.message_page?.has_more)
    messageBeforeId.value = teamDetail.value?.message_page?.next_before_id ?? null
    messageTotal.value = teamDetail.value?.message_page?.total ?? messages.value.length
  },
  { immediate: true },
)

watch(
  () => teamDetail.value?.message_page,
  (page) => {
    if (!page) return
    messageTotal.value = page.total
    if (olderMessages.value.length) return
    messageHasMore.value = Boolean(page.has_more)
    messageBeforeId.value = page.next_before_id ?? null
  },
  { deep: true },
)

function formatTime(value?: string | null): string {
  const timestamp = parseUtcTimestamp(value)
  return Number.isFinite(timestamp) ? formatDateTime(timestamp, 'HH:mm') : ''
}

function eventLabel(event: AgentTeamEvent): string {
  if (event.from_status && event.to_status) return `${event.from_status} -> ${event.to_status}`
  return event.event_type
}

function taskMember(task: AgentTeamTask): string {
  return task.member_key || members.value.find((member) => member.member_id === task.member_id)?.display_name || '未分配'
}

function formatFullDetail(value: unknown): string {
  if (value === undefined || value === null) return ''
  try {
    return typeof value === 'string' ? value : JSON.stringify(value) || ''
  } catch {
    return String(value)
  }
}

function hasTaskEvidence(task: AgentTeamTask): boolean {
  return Boolean(
    task.result
    || task.artifacts?.length
    || task.errors?.length
    || task.attempt_count > 0,
  )
}
</script>

<template>
  <section v-if="team" class="agent-team-trace" aria-label="小菱子 Agent 协作团队">
    <header class="agent-team-trace-header">
      <div v-if="embedded" class="agent-team-embedded-title">
        <span class="agent-team-title">{{ team.title }}</span>
        <span class="agent-team-status" :class="statusClass(team.status)">{{ label(team.status) }}</span>
      </div>
      <button
        v-else
        class="agent-team-toggle"
        type="button"
        :aria-expanded="expanded"
        :aria-controls="tracePanelId"
        :aria-label="`${expanded ? '收起' : '展开'}${team.title}团队的子 Agent 协作过程，团队编号 ${team.team_id}`"
        @click="toggle"
      >
        <span class="agent-team-caret" :class="{ 'is-open': expanded }" aria-hidden="true">›</span>
        <span class="agent-team-title">{{ team.title }}</span>
        <span class="agent-team-status" :class="statusClass(team.status)">{{ label(team.status) }}</span>
      </button>
      <span class="agent-team-refresh" :class="{ 'is-loading': loading }" role="status">
        {{ loading ? '同步中' : `${counts.completed}/${counts.total} 完成` }}
      </span>
      <button
        class="agent-team-open-detail"
        type="button"
        title="在独立悬浮窗中查看该团队的完整工作内容"
        aria-label="查看团队详情"
        @click="openDetail"
      >查看详情</button>
    </header>

    <div v-if="progressPercent !== null || problemCount > 0" class="agent-team-progress" aria-label="团队审查进度">
      <div v-if="progressPercent !== null" class="agent-team-progress-topline">
        <span class="agent-team-progress-count">{{ counts.completed }}/{{ counts.total }} 完成</span>
        <span class="agent-team-progress-label">{{ progressPercent }}%</span>
      </div>
      <div
        v-if="progressPercent !== null"
        class="agent-team-progress-track"
        role="progressbar"
        :aria-valuemin="0"
        :aria-valuemax="100"
        :aria-valuenow="progressPercent"
        :aria-label="`团队任务完成 ${counts.completed}/${counts.total}`"
      >
        <span class="agent-team-progress-fill" :style="{ width: `${progressPercent}%` }"></span>
      </div>
      <span v-if="problemCount > 0" class="agent-team-progress-problems" role="status">失败/阻塞 {{ problemCount }}</span>
    </div>

    <p v-if="error" class="agent-team-error" role="status">{{ error }}</p>

    <div :id="tracePanelId" class="agent-team-trace-panel">
      <Transition name="trace-expand">
        <div v-if="detailsExpanded" class="agent-team-trace-body" data-layout="in-flow">
          <div class="agent-team-trace-content">
      <div class="agent-team-stats" aria-label="团队任务统计">
        <span><b>{{ counts.completed }}</b>完成</span>
        <span><b>{{ counts.running }}</b>运行</span>
        <span><b>{{ counts.queued }}</b>排队</span>
        <span><b>{{ counts.failed + counts.blocked }}</b>失败/阻塞</span>
      </div>

      <section v-if="tasks.length" class="agent-team-section" aria-label="任务依赖星图">
        <h4>依赖星图</h4>
        <div v-if="dependencyGraph" class="agent-team-dependency-map-wrap">
          <svg
            class="agent-team-dependency-map"
            :viewBox="`0 0 1000 ${dependencyGraph.height}`"
            role="img"
            :aria-label="dependencyGraph.labelText"
            preserveAspectRatio="xMinYMin meet"
          >
            <defs>
              <marker :id="dependencyMarkerId" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto" markerUnits="strokeWidth">
                <path d="M0,0 L8,4 L0,8 z" />
              </marker>
            </defs>
            <path
              v-for="(edge, edgeIndex) in dependencyGraph.edges"
              :key="`${edge.sourceKey}->${edge.targetKey}`"
              :d="edge.path"
              :data-dependency-edge="`${edge.sourceKey}->${edge.targetKey}`"
              class="agent-team-dependency-edge"
              :class="{
                'is-traced': tracedTaskKeys.has(edge.sourceKey) && tracedTaskKeys.has(edge.targetKey),
                'is-dimmed': Boolean(selectedRoot && (!tracedTaskKeys.has(edge.sourceKey) || !tracedTaskKeys.has(edge.targetKey))),
              }"
              :style="{ '--trace-delay': `${Math.min(edgeIndex * 32, 480)}ms` }"
              :marker-end="`url(#${dependencyMarkerId})`"
            />
            <g
              v-for="(node, nodeIndex) in dependencyGraph.nodes"
              :key="node.task.task_key"
              :data-task-node="node.task.task_key"
              :data-task-root="node.isRoot ? node.task.task_key : undefined"
              class="agent-team-dependency-node"
              :class="{
                'is-root': node.isRoot,
                'is-traced': tracedTaskKeys.has(node.task.task_key),
                'is-dimmed': Boolean(selectedRoot && !tracedTaskKeys.has(node.task.task_key)),
              }"
              :style="{ '--trace-delay': `${Math.min(dependencyGraph.edges.length * 32, 480) + Math.min(nodeIndex * 32, 384)}ms` }"
              :transform="`translate(${node.x - 102}, ${node.y - 24})`"
            >
              <title>{{ node.task.title }} · {{ node.isRoot ? '根节点，无前置任务' : `依赖 ${node.task.depends_on.join('、')}` }}</title>
              <rect width="204" height="48" rx="12" />
              <circle cx="17" cy="17" r="6" />
              <text x="31" y="20" class="agent-team-dependency-title">{{ node.task.title.length > 22 ? `${node.task.title.slice(0, 22)}…` : node.task.title }}</text>
              <text x="31" y="37" class="agent-team-dependency-meta">{{ node.isRoot ? '根节点 · 无前置任务' : `依赖 ${node.task.depends_on.length} 项` }}</text>
            </g>
          </svg>
          <p class="agent-team-dependency-roots">根节点溯源：{{ dependencyGraph.roots.length ? dependencyGraph.roots.map((node) => node.task.title).join('、') : '没有无前置依赖的任务' }}</p>
          <div v-if="dependencyGraph.roots.length" class="agent-team-root-trace" role="group" aria-label="从根节点追溯任务链路">
            <span>从根节点追溯</span>
            <button
              v-for="root in dependencyGraph.roots"
              :key="`trace-root-${root.task.task_key}`"
              type="button"
              :aria-label="`从根节点 ${root.task.title} 追溯后续任务`"
              :aria-pressed="selectedRootKey === root.task.task_key"
              @click="traceFromRoot(root.task.task_key)"
            >{{ root.task.title }}</button>
            <button
              v-if="selectedRoot"
              type="button"
              class="agent-team-root-trace-clear"
              aria-label="清除根节点追溯"
              @click="selectedRootKey = null"
            >清除</button>
          </div>
          <p v-if="selectedRoot" class="agent-team-root-trace-status" role="status" aria-live="polite">
            正在从“{{ selectedRoot.task.title }}”追溯：{{ tracedTaskKeys.size }} 个可达任务节点。
          </p>
          <ul class="agent-team-dependency-text" aria-label="任务依赖文字说明">
            <li v-for="node in dependencyGraph.nodes" :key="`dependency-text-${node.task.task_key}`">
              {{ node.isRoot ? `根节点：${node.task.title}，无前置任务。` : `${node.task.title} 依赖：${node.task.depends_on.join('、')}。` }}
            </li>
          </ul>
        </div>
        <p v-else class="agent-team-dependency-empty" role="status">依赖链路暂不可读，星图未绘制。</p>
      </section>

      <section v-if="members.length" class="agent-team-section" aria-label="团队成员">
        <h4>成员</h4>
        <div class="agent-team-member-cards">
          <AgentMemberWorkCard
            v-for="member in members"
            :key="member.member_id"
            :member="member"
            :tasks="tasks"
            :events="events"
            :team-started-at="teamDetail?.started_at ?? null"
          />
        </div>
      </section>

      <section v-if="tasks.length" class="agent-team-section" aria-label="任务依赖">
        <h4>任务与依赖</h4>
        <div class="agent-team-record-viewport" aria-label="任务依赖浏览区">
          <ol class="agent-team-tasks">
            <li v-for="task in visibleTasks" :key="task.task_id" class="agent-team-task" :class="statusClass(task.status)">
              <div class="agent-team-task-line">
                <strong>{{ task.title }}</strong>
                <span class="agent-team-item-status">{{ label(task.status) }}</span>
              </div>
              <small>{{ taskMember(task) }} · 第 {{ task.attempt_count }}/{{ task.max_attempts }} 次</small>
              <div v-if="Array.isArray(task.depends_on) && task.depends_on.length" class="agent-team-dependencies">
                依赖: <code v-for="dependency in task.depends_on" :key="dependency">{{ dependency }}</code>
              </div>
              <details v-if="task.instructions?.trim()" class="agent-team-evidence agent-team-instructions">
                <summary>小菱派发内容</summary>
                <div class="agent-team-detail-row">
                  <b>{{ taskMember(task) }}</b>
                  <code>{{ task.instructions }}</code>
                </div>
              </details>
              <details v-if="hasTaskEvidence(task)" class="agent-team-evidence">
                <summary>证据与错误</summary>
                <div v-if="task.result" class="agent-team-detail-row">
                  <b>结果</b><code>{{ formatFullDetail(task.result) }}</code>
                </div>
                <div v-if="task.artifacts?.length" class="agent-team-detail-row">
                  <b>产物</b><code>{{ formatFullDetail(task.artifacts) }}</code>
                </div>
                <div v-if="task.errors?.length" class="agent-team-detail-row is-error">
                  <b>错误</b><code>{{ formatFullDetail(task.errors) }}</code>
                </div>
              </details>
            </li>
          </ol>
        </div>
        <div v-if="tasks.length > TRACE_PAGE_SIZE" class="agent-team-pager" aria-label="任务分段浏览">
          <span>显示 {{ visibleTasks.length }}/{{ tasks.length }} 项</span>
          <div class="agent-team-pager-actions">
            <button
              v-if="visibleTasks.length < tasks.length"
              class="agent-team-page-action"
              type="button"
              aria-label="查看更多任务"
              @click="showMore('task')"
            >查看更多</button>
            <button
              v-if="visibleTasks.length > TRACE_PAGE_SIZE"
              class="agent-team-page-action"
              type="button"
              aria-label="收起任务"
              @click="collapseList('task')"
            >收起</button>
          </div>
        </div>
      </section>

      <section v-if="events.length" class="agent-team-section" aria-label="协作事件">
        <h4>事件</h4>
        <div class="agent-team-record-viewport" aria-label="协作事件浏览区">
          <ul class="agent-team-events">
            <li v-for="event in visibleEvents" :key="`event-${event.event_id}`" class="agent-team-event">
              <time>{{ formatTime(event.created_at) }}</time>
              <span>{{ eventLabel(event) }}</span>
              <code v-if="event.trace_id">{{ event.trace_id }}</code>
              <details v-if="event.detail" class="agent-team-record-detail">
                <summary>查看事件详情</summary>
                <code>{{ formatFullDetail(event.detail) }}</code>
              </details>
            </li>
          </ul>
        </div>
        <div v-if="events.length > TRACE_PAGE_SIZE" class="agent-team-pager" aria-label="事件分段浏览">
          <span>最近 {{ visibleEvents.length }}/{{ events.length }} 项</span>
          <div class="agent-team-pager-actions">
            <button
              v-if="visibleEvents.length < events.length"
              class="agent-team-page-action"
              type="button"
              aria-label="查看更多早期事件"
              @click="showMore('event')"
            >查看更多</button>
            <button
              v-if="visibleEvents.length > TRACE_PAGE_SIZE"
              class="agent-team-page-action"
              type="button"
              aria-label="收起事件"
              @click="collapseList('event')"
            >收起</button>
          </div>
        </div>
      </section>

      <section v-if="messages.length" class="agent-team-section" aria-label="协作消息">
        <h4>消息</h4>
        <div class="agent-team-record-viewport" aria-label="协作消息浏览区">
          <ul class="agent-team-events">
            <li v-for="message in visibleMessages" :key="`message-${message.message_id}`" class="agent-team-message">
              <time>{{ formatTime(message.create_time ?? message.created_at) }}</time>
              <span>{{ message.subject || message.message_type }}</span>
              <small>{{ message.sent_from }} -> {{ message.send_to }} · {{ message.message_id }}</small>
              <code class="agent-team-message-trace">{{ message.trace_id }} / {{ message.correlation_id }} / {{ message.causation_id }}</code>
              <details v-if="message.payload || message.context || message.artifacts?.length || message.errors?.length" class="agent-team-record-detail">
                <summary>查看消息详情</summary>
                <code>{{ formatFullDetail({ payload: message.payload, context: message.context, artifacts: message.artifacts, errors: message.errors }) }}</code>
              </details>
            </li>
          </ul>
        </div>
        <p v-if="messageHistoryError" class="agent-team-history-error" role="status">{{ messageHistoryError }}</p>
        <div v-if="messages.length > TRACE_PAGE_SIZE || messageHasMore" class="agent-team-pager" aria-label="消息分段浏览">
          <span>最近 {{ visibleMessages.length }}/{{ messageTotal || messages.length }} 项</span>
          <div class="agent-team-pager-actions">
            <button
              v-if="visibleMessages.length < messages.length || messageHasMore"
              class="agent-team-page-action"
              type="button"
              aria-label="查看更多早期消息"
              :disabled="messageHistoryLoading"
              @click="showMore('message')"
            >{{ messageHistoryLoading ? '加载中' : '查看更多' }}</button>
            <button
              v-if="visibleMessages.length > TRACE_PAGE_SIZE"
              class="agent-team-page-action"
              type="button"
              aria-label="收起消息"
              @click="collapseList('message')"
            >收起</button>
          </div>
        </div>
      </section>

      <p v-if="!members.length && !tasks.length && !events.length && !messages.length" class="agent-team-empty">
        团队详情正在同步
      </p>
          </div>
        </div>
      </Transition>
    </div>
  </section>
</template>

<style scoped>
.agent-team-trace { box-sizing: border-box; width: 100%; min-width: 0; margin-top: 8px; overflow: hidden; border: 1px solid var(--gray-200); border-radius: 8px; background: #fff; color: var(--gray-800); font-size: 12px; }
.agent-team-trace-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-width: 0; padding: 8px 10px; }
.agent-team-toggle { display: flex; align-items: center; gap: 5px; min-width: 0; flex: 1; border: 0; background: transparent; color: inherit; cursor: pointer; text-align: left; padding: 0; }
.agent-team-caret { flex: none; color: var(--gray-400); font-size: 18px; line-height: 14px; transition: transform var(--motion-duration-fast, 160ms) var(--motion-ease-standard, ease); transform: rotate(0deg); }
.agent-team-caret.is-open { transform: rotate(90deg); }
.agent-team-title { min-width: 0; overflow-wrap: anywhere; font-weight: 650; }
.agent-team-status, .agent-team-item-status { flex: none; white-space: nowrap; color: var(--gray-500); font-size: 10px; }
.agent-team-status { padding: 1px 6px; border: 1px solid var(--gray-200); border-radius: 999px; }
.agent-team-status.is-running, .agent-team-status.is-verifying { color: var(--brand-600); border-color: var(--brand-200); background: var(--brand-50); }
.agent-team-status.is-completed { color: var(--color-success); border-color: var(--color-success); background: var(--color-success-light); }
.agent-team-status.is-failed, .agent-team-status.is-expired { color: var(--color-danger); border-color: var(--color-danger); }
.agent-team-refresh { flex: none; color: var(--gray-500); font-size: 10px; white-space: nowrap; }
.agent-team-refresh.is-loading { color: var(--brand-500); }
.agent-team-progress { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 5px 10px; padding: 0 10px 9px; }
.agent-team-progress-topline { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; grid-column: 1 / -1; color: var(--gray-500); font-size: 10.5px; }
.agent-team-progress-label { color: var(--brand-600); font-size: 12px; font-weight: 700; }
.agent-team-progress-track { height: 6px; overflow: hidden; border-radius: 999px; background: var(--gray-100); }
.agent-team-progress-fill { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, var(--brand-500), var(--accent-500)); transition: width 180ms ease-out; }
.agent-team-progress-problems { color: var(--color-danger); font-size: 10.5px; white-space: nowrap; }
.agent-team-open-detail {
  flex: none;
  min-height: 32px;
  padding: 4px 10px;
  border: 1px solid var(--gray-300);
  border-radius: 999px;
  background: #fff;
  color: var(--brand-600);
  font-size: 11px;
  line-height: 1.4;
  cursor: pointer;
  white-space: nowrap;
}
.agent-team-open-detail:hover { border-color: var(--brand-300); background: var(--brand-50); }
.agent-team-error { margin: 0; padding: 0 10px 8px; color: var(--color-danger); overflow-wrap: anywhere; }
.agent-team-history-error { margin: 5px 0 0; color: var(--color-danger); overflow-wrap: anywhere; }
.agent-team-trace-panel { min-width: 0; }
.agent-team-trace-body { display: grid; grid-template-rows: 1fr; min-width: 0; min-height: 0; overflow: hidden; opacity: 1; }
.trace-expand-enter-active, .trace-expand-leave-active { transition: grid-template-rows var(--motion-duration-standard, 280ms) var(--motion-ease-standard, ease), opacity var(--motion-duration-fast, 160ms) ease; }
.trace-expand-enter-from, .trace-expand-leave-to { grid-template-rows: 0fr; opacity: 0; }
.trace-expand-enter-to, .trace-expand-leave-from { grid-template-rows: 1fr; opacity: 1; }
.agent-team-trace-content { min-width: 0; min-height: 0; overflow: hidden; padding: 0 10px 10px; border-top: 1px solid var(--gray-100); }
.agent-team-dependency-map-wrap { position: relative; min-width: 0; overflow-x: auto; overscroll-behavior-inline: contain; }
.agent-team-dependency-map { display: block; width: 100%; min-width: 600px; height: auto; overflow: visible; }
.agent-team-dependency-edge {
  fill: none;
  stroke: #8C85F2;
  stroke-width: 2;
  opacity: .62;
  stroke-dasharray: 2000;
  stroke-dashoffset: 2000;
  animation: trace-edge-grow var(--motion-duration-emphasis, 520ms) var(--motion-ease-standard, ease) var(--trace-delay, 0ms) both;
  transition: opacity var(--motion-duration-fast, 160ms) ease, stroke-width var(--motion-duration-fast, 160ms) ease;
}
@keyframes trace-edge-grow { from { stroke-dashoffset: 2000; } to { stroke-dashoffset: 0; } }
.agent-team-dependency-map marker path { fill: #675BEE; }
.agent-team-dependency-node { opacity: 0; animation: trace-node-reveal var(--motion-duration-standard, 280ms) var(--motion-ease-standard, ease) var(--trace-delay, 0ms) both; transition: opacity var(--motion-duration-fast, 160ms) ease; }
@keyframes trace-node-reveal { from { opacity: 0; filter: drop-shadow(0 0 0 rgba(103,91,238,0)); } to { opacity: 1; filter: drop-shadow(0 3px 5px rgba(103,91,238,.12)); } }
.agent-team-dependency-node rect { fill: #fff; stroke: #DEE1ED; stroke-width: 1.4; transition: stroke var(--motion-duration-fast, 160ms) ease, stroke-width var(--motion-duration-fast, 160ms) ease, fill var(--motion-duration-fast, 160ms) ease; }
.agent-team-dependency-node circle { fill: #675BEE; }
.agent-team-dependency-node.is-root rect { fill: #F4F3FF; stroke: #A9A4F8; }
.agent-team-dependency-node.is-root circle { fill: #32A987; }
.agent-team-dependency-edge.is-traced { stroke: #5146D9; stroke-width: 3.4; opacity: 1; filter: drop-shadow(0 1px 3px rgba(81,70,217,.42)); }
.agent-team-dependency-edge.is-dimmed { stroke: #AEB2C1; opacity: .16; }
.agent-team-dependency-node.is-traced rect { fill: #EFEEFF; stroke: #675BEE; stroke-width: 2.6; }
.agent-team-dependency-node.is-traced circle { fill: #1D9B78; }
.agent-team-dependency-node.is-dimmed > * { opacity: .24; }
.agent-team-dependency-title { fill: #31364A; font-size: 13px; font-weight: 650; }
.agent-team-dependency-meta { fill: #777E94; font-size: 10px; }
.agent-team-dependency-roots { margin: 4px 0 0; color: var(--gray-500); font-size: 10.5px; overflow-wrap: anywhere; }
.agent-team-root-trace { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; margin-top: 7px; color: var(--gray-500); font-size: 10px; }
.agent-team-root-trace button { min-height: 30px; padding: 4px 8px; border: 1px solid var(--gray-300); border-radius: 999px; background: #fff; color: var(--brand-600); font: inherit; cursor: pointer; }
.agent-team-root-trace button:hover, .agent-team-root-trace button[aria-pressed='true'] { border-color: var(--brand-400); background: var(--brand-50); color: #5146D9; }
.agent-team-root-trace button[aria-pressed='true'] { box-shadow: inset 0 0 0 1px rgba(103,91,238,.16); font-weight: 650; }
.agent-team-root-trace .agent-team-root-trace-clear { border-color: transparent; background: transparent; color: var(--gray-500); text-decoration: underline; }
.agent-team-root-trace-status { margin: 5px 0 0; color: var(--brand-600); font-size: 10px; }
.agent-team-dependency-empty { margin: 0; padding: 10px; border: 1px dashed var(--gray-300); border-radius: 6px; color: var(--gray-500); text-align: center; }
.agent-team-dependency-text { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
.agent-team-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 4px; padding: 8px 0; color: var(--gray-500); text-align: center; }
.agent-team-stats span { min-width: 0; overflow-wrap: anywhere; }
.agent-team-stats b { display: block; color: var(--gray-900); font-size: 14px; line-height: 17px; }
.agent-team-section { min-width: 0; padding-top: 7px; }
.agent-team-section h4 { margin: 0 0 5px; color: var(--gray-600); font-size: 11px; font-weight: 650; }
.agent-team-member-cards { display: grid; gap: 8px; }
.agent-team-members, .agent-team-tasks, .agent-team-events { display: grid; gap: 4px; margin: 0; padding: 0; list-style: none; }
.agent-team-record-viewport { min-width: 0; max-block-size: 264px; overflow-x: hidden; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
.agent-team-member, .agent-team-task { display: grid; grid-template-columns: 8px minmax(0, 1fr) auto auto; align-items: center; gap: 5px; min-width: 0; padding: 5px 6px; border: 1px solid var(--gray-100); border-radius: 5px; }
.agent-team-member-name, .agent-team-task strong { min-width: 0; overflow-wrap: anywhere; }
.agent-team-member code, .agent-team-task code, .agent-team-events code { min-width: 0; overflow-wrap: anywhere; color: var(--brand-600); font-size: 10px; }
.agent-team-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--gray-300); }
.agent-team-dot.is-running { background: var(--brand-500); }
.agent-team-dot.is-completed { background: var(--color-success); }
.agent-team-dot.is-failed, .agent-team-dot.is-dead_letter, .agent-team-dot.is-expired { background: var(--color-danger); }
.agent-team-task { display: block; }
.agent-team-task-line { display: flex; align-items: baseline; justify-content: space-between; gap: 6px; min-width: 0; }
.agent-team-task small { display: block; margin-top: 2px; color: var(--gray-500); overflow-wrap: anywhere; }
.agent-team-dependencies { margin-top: 3px; color: var(--gray-500); overflow-wrap: anywhere; }
.agent-team-dependencies code { margin-left: 3px; }
.agent-team-events li { display: grid; grid-template-columns: 36px minmax(0, 1fr); gap: 4px 6px; min-width: 0; padding: 3px 0; border-bottom: 1px solid var(--gray-100); }
.agent-team-events time { color: var(--gray-400); font-size: 10px; }
.agent-team-events span, .agent-team-events small { min-width: 0; overflow-wrap: anywhere; }
.agent-team-events small { grid-column: 2; color: var(--gray-500); font-size: 10px; }
.agent-team-pager { display: flex; align-items: center; gap: 6px; min-height: 36px; color: var(--gray-500); font-size: 10px; }
.agent-team-pager-actions { display: flex; gap: 4px; margin-left: auto; }
.agent-team-page-action { min-width: 56px; min-height: 32px; border: 1px solid var(--gray-300); border-radius: 4px; background: #fff; color: var(--brand-600); cursor: pointer; font-size: 11px; line-height: 1.2; }
.agent-team-page-action:hover { border-color: var(--brand-300); background: var(--brand-50); }
.agent-team-toggle:focus-visible, .agent-team-open-detail:focus-visible, .agent-team-page-action:focus-visible, .agent-team-root-trace button:focus-visible { outline: 2px solid var(--brand-300); outline-offset: 2px; }
.agent-team-record-detail { grid-column: 2; min-width: 0; color: var(--gray-500); }
.agent-team-record-detail summary { cursor: pointer; overflow-wrap: anywhere; font-size: 10px; }
.agent-team-record-detail code { display: block; margin-top: 4px; color: var(--gray-700); white-space: pre-wrap; }
.agent-team-evidence { margin-top: 5px; color: var(--gray-500); }
.agent-team-evidence summary { cursor: pointer; color: var(--brand-600); font-size: 10px; }
.agent-team-detail-row { display: grid; grid-template-columns: 32px minmax(0, 1fr); gap: 5px; margin-top: 4px; min-width: 0; }
.agent-team-detail-row code { overflow-wrap: anywhere; color: var(--gray-700); white-space: pre-wrap; }
.agent-team-detail-row.is-error code { color: var(--color-danger); }
.agent-team-message-trace { grid-column: 2; overflow-wrap: anywhere; color: var(--gray-500); font-size: 9px; }
.agent-team-empty { margin: 8px 0 0; color: var(--gray-500); }
@media (max-width: 420px) {
  .agent-team-trace-header { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; }
  .agent-team-toggle { grid-column: 1 / -1; width: 100%; }
  .agent-team-refresh { grid-column: 1; }
  .agent-team-open-detail { grid-column: 2; }
  .agent-team-progress { grid-template-columns: minmax(0, 1fr) auto; }
  .agent-team-member { grid-template-columns: 8px minmax(0, 1fr) auto; }
  .agent-team-member code { grid-column: 2 / -1; }
  .agent-team-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); row-gap: 8px; }
  .agent-team-open-detail, .agent-team-page-action { min-height: 40px; }
}
@media (prefers-reduced-motion: reduce) {
  .agent-team-progress-fill { transition: none; }
  .agent-team-caret { transition: none; }
  .trace-expand-enter-active, .trace-expand-leave-active { transition: none; }
  .agent-team-dependency-edge { animation: none; stroke-dasharray: none; stroke-dashoffset: 0; transition: none; }
  .agent-team-dependency-node { animation: none; opacity: 1; filter: none; transition: none; }
  .agent-team-dependency-node rect { transition: none; }
}
</style>
