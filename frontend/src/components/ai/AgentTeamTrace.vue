<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { listAgentTeamMessages } from '@/api/agentTeams'
import type {
  AgentTeamDetail,
  AgentTeamEvent,
  AgentTeamMember,
  AgentTeamMessage,
  AgentTeamSummary,
  AgentTeamTask,
} from '@/api/agentTeams'

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
  if (!value) return ''
  const timestamp = new Date(value)
  if (Number.isNaN(timestamp.getTime())) return ''
  return timestamp.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
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

function formatDetail(value: unknown, limit = 700): string {
  const text = formatFullDetail(value)
  return text.length > limit ? `${text.slice(0, limit)}...` : text
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
      <button
        class="agent-team-toggle"
        type="button"
        :aria-expanded="expanded"
        :aria-label="expanded ? '收起子 Agent 协作过程' : '展开子 Agent 协作过程'"
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

    <p v-if="error" class="agent-team-error" role="status">{{ error }}</p>

    <div v-if="expanded" class="agent-team-trace-body">
      <div class="agent-team-stats" aria-label="团队任务统计">
        <span><b>{{ counts.completed }}</b>完成</span>
        <span><b>{{ counts.running }}</b>运行</span>
        <span><b>{{ counts.queued }}</b>排队</span>
        <span><b>{{ counts.failed + counts.blocked }}</b>失败/阻塞</span>
      </div>

      <section v-if="members.length" class="agent-team-section" aria-label="团队成员">
        <h4>成员</h4>
        <ul class="agent-team-members">
          <li v-for="member in members" :key="member.member_id" class="agent-team-member">
            <span class="agent-team-dot" :class="statusClass(member.status)" aria-hidden="true"></span>
            <span class="agent-team-member-name">{{ member.display_name }}</span>
            <code>{{ member.address }}</code>
            <span class="agent-team-item-status">{{ label(member.status) }}</span>
          </li>
        </ul>
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
              <div v-if="task.depends_on.length" class="agent-team-dependencies">
                依赖: <code v-for="dependency in task.depends_on" :key="dependency">{{ dependency }}</code>
              </div>
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
                <summary>{{ formatDetail(event.detail, 360) }}</summary>
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
                <summary>{{ formatDetail({ payload: message.payload, context: message.context }, 520) }}</summary>
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
  </section>
</template>

<style scoped>
.agent-team-trace { box-sizing: border-box; width: 100%; min-width: 0; margin-top: 8px; overflow: hidden; border: 1px solid #dfe3e8; border-radius: 8px; background: #fff; color: #1f2329; font-size: 12px; }
.agent-team-trace-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-width: 0; padding: 8px 10px; }
.agent-team-toggle { display: flex; align-items: center; gap: 5px; min-width: 0; flex: 1; border: 0; background: transparent; color: inherit; cursor: pointer; text-align: left; padding: 0; }
.agent-team-caret { flex: none; color: #8b949e; font-size: 18px; line-height: 14px; transition: transform .15s ease; transform: rotate(0deg); }
.agent-team-caret.is-open { transform: rotate(90deg); }
.agent-team-title { min-width: 0; overflow-wrap: anywhere; font-weight: 650; }
.agent-team-status, .agent-team-item-status { flex: none; white-space: nowrap; color: #66707a; font-size: 10px; }
.agent-team-status { padding: 1px 6px; border: 1px solid #dfe3e8; border-radius: 999px; }
.agent-team-status.is-running, .agent-team-status.is-verifying { color: #1769aa; border-color: #a9cae5; background: #eff7ff; }
.agent-team-status.is-completed { color: #26734d; border-color: #b7dec7; background: #f0faf3; }
.agent-team-status.is-failed, .agent-team-status.is-expired { color: #a73832; border-color: #efbbb5; background: #fff3f1; }
.agent-team-refresh { flex: none; color: #7a838f; font-size: 10px; white-space: nowrap; }
.agent-team-refresh.is-loading { color: #3978d6; }
.agent-team-open-detail {
  flex: none;
  border: 1px solid #cfd7df;
  border-radius: 999px;
  background: #fff;
  color: #3978d6;
  font-size: 10px;
  line-height: 1;
  padding: 4px 8px;
  cursor: pointer;
  white-space: nowrap;
}
.agent-team-open-detail:hover { border-color: #79a8df; background: #f4f9ff; }
.agent-team-error { margin: 0; padding: 0 10px 8px; color: #b42318; overflow-wrap: anywhere; }
.agent-team-history-error { margin: 5px 0 0; color: #b42318; overflow-wrap: anywhere; }
.agent-team-trace-body { min-width: 0; padding: 0 10px 10px; border-top: 1px solid #edf0f2; }
.agent-team-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 4px; padding: 8px 0; color: #737b85; text-align: center; }
.agent-team-stats span { min-width: 0; overflow-wrap: anywhere; }
.agent-team-stats b { display: block; color: #20252b; font-size: 14px; line-height: 17px; }
.agent-team-section { min-width: 0; padding-top: 7px; }
.agent-team-section h4 { margin: 0 0 5px; color: #5e6873; font-size: 11px; font-weight: 650; }
.agent-team-members, .agent-team-tasks, .agent-team-events { display: grid; gap: 4px; margin: 0; padding: 0; list-style: none; }
.agent-team-record-viewport { min-width: 0; max-block-size: 264px; overflow-x: hidden; overflow-y: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
.agent-team-member, .agent-team-task { display: grid; grid-template-columns: 8px minmax(0, 1fr) auto auto; align-items: center; gap: 5px; min-width: 0; padding: 5px 6px; border: 1px solid #edf0f2; border-radius: 5px; }
.agent-team-member-name, .agent-team-task strong { min-width: 0; overflow-wrap: anywhere; }
.agent-team-member code, .agent-team-task code, .agent-team-events code { min-width: 0; overflow-wrap: anywhere; color: #1756a9; font-size: 10px; }
.agent-team-dot { width: 7px; height: 7px; border-radius: 50%; background: #a6aeb8; }
.agent-team-dot.is-running { background: #3978d6; }
.agent-team-dot.is-completed { background: #2b8a57; }
.agent-team-dot.is-failed, .agent-team-dot.is-dead_letter, .agent-team-dot.is-expired { background: #c43d36; }
.agent-team-task { display: block; }
.agent-team-task-line { display: flex; align-items: baseline; justify-content: space-between; gap: 6px; min-width: 0; }
.agent-team-task small { display: block; margin-top: 2px; color: #7a838f; overflow-wrap: anywhere; }
.agent-team-dependencies { margin-top: 3px; color: #7a838f; overflow-wrap: anywhere; }
.agent-team-dependencies code { margin-left: 3px; }
.agent-team-events li { display: grid; grid-template-columns: 36px minmax(0, 1fr); gap: 4px 6px; min-width: 0; padding: 3px 0; border-bottom: 1px solid #f1f2f4; }
.agent-team-events time { color: #8a929d; font-size: 10px; }
.agent-team-events span, .agent-team-events small { min-width: 0; overflow-wrap: anywhere; }
.agent-team-events small { grid-column: 2; color: #7a838f; font-size: 10px; }
.agent-team-pager { display: flex; align-items: center; gap: 6px; min-height: 28px; color: #7a838f; font-size: 10px; }
.agent-team-pager-actions { display: flex; gap: 4px; margin-left: auto; }
.agent-team-page-action { min-width: 48px; min-height: 24px; border: 1px solid #cfd7df; border-radius: 4px; background: #fff; color: #1756a9; cursor: pointer; font-size: 10px; line-height: 1; }
.agent-team-page-action:hover { border-color: #79a8df; background: #f4f9ff; }
.agent-team-record-detail { grid-column: 2; min-width: 0; color: #69727d; }
.agent-team-record-detail summary { cursor: pointer; overflow-wrap: anywhere; font-size: 10px; }
.agent-team-record-detail code { display: block; margin-top: 4px; color: #3c4652; white-space: pre-wrap; }
.agent-team-evidence { margin-top: 5px; color: #69727d; }
.agent-team-evidence summary { cursor: pointer; color: #1756a9; font-size: 10px; }
.agent-team-detail-row { display: grid; grid-template-columns: 32px minmax(0, 1fr); gap: 5px; margin-top: 4px; min-width: 0; }
.agent-team-detail-row code { overflow-wrap: anywhere; color: #3c4652; white-space: pre-wrap; }
.agent-team-detail-row.is-error code { color: #a73832; }
.agent-team-message-trace { grid-column: 2; overflow-wrap: anywhere; color: #69727d; font-size: 9px; }
.agent-team-empty { margin: 8px 0 0; color: #7a838f; }
@media (max-width: 420px) {
  .agent-team-member { grid-template-columns: 8px minmax(0, 1fr) auto; }
  .agent-team-member code { grid-column: 2 / -1; }
  .agent-team-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); row-gap: 8px; }
}
</style>
