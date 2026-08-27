<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import type {
  AgentTeamEvent,
  AgentTeamMember,
  AgentTeamTask,
} from '@/api/agentTeams'

/**
 * Codex 风格「子 Agent 工作卡片」:一个子 Agent 一张卡。
 *
 * 结构参照被认可的 Codex 多 Agent 界面:
 *   [彩色头像] 名称 · 角色徽标          [追问]
 *              状态行(已开始工作/思考中…/已完成/出错了)
 *              实时计时(已工作 1分41秒,运行中每秒刷新)
 *              等宽操作日志(最近 3 条:开始工作/完成任务…)
 *
 * 与旧「成员药丸」的区别:垂直信息密度更高、带实时计时与
 * 逐条日志,让用户像看 Codex 一样看到每个子 Agent 在干什么。
 */
const props = withDefaults(defineProps<{
  member: AgentTeamMember
  tasks?: AgentTeamTask[]
  events?: AgentTeamEvent[]
  /** 团队开始时间:成员缺 started_at 时的计时兜底。 */
  teamStartedAt?: string | null
  /** 是否展示「追问」按钮(团队悬浮窗内)。 */
  showAsk?: boolean
  /** 最多展示的操作日志条数。 */
  logLimit?: number
}>(), {
  tasks: () => [],
  events: () => [],
  teamStartedAt: null,
  showAsk: false,
  logLimit: 3,
})

const emit = defineEmits<{ ask: [member: AgentTeamMember] }>()

const ROLE_LABELS: Record<string, string> = {
  worker: '执行', verifier: '验证', summarizer: '汇总',
}

/** 事件类型 → 中文动作短语(Codex 的 started working / completed 对应物)。 */
const EVENT_ACTION_TEXT: Record<string, string> = {
  'task.claimed': '开始工作',
  'task.completed': '完成',
  'task.failed': '失败',
  'task.dead_letter': '失败(不再重试)',
  'task.expired': '过期',
  'task.queued': '排队',
  'task.retry_queued': '重新排队',
  'task.blocked': '被阻塞',
  'task.cancelled': '被取消',
  'member.created': '已创建',
  'member.started': '启动',
  'member.completed': '收工',
  'member.failed': '出错',
  'member.reclaimed': '被回收',
}

function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role
}

const theme = computed<'manager' | 'worker' | 'verifier' | 'summarizer'>(() => (
  ROLE_LABELS[props.member.role] ? props.member.role : 'manager'
))

const initial = computed(() => (props.member.display_name || '员').trim().charAt(0))

/* ── 实时计时:运行中每秒刷新,结束态定格为总耗时 ────────── */
const now = ref(Date.now())
let tickTimer: number | undefined
const isRunning = computed(() => props.member.status === 'running')

function stopTick(): void {
  if (tickTimer !== undefined) {
    window.clearInterval(tickTimer)
    tickTimer = undefined
  }
}

// 成员从排队→运行中后才启动计时:挂在挂载时机上会漏掉,必须跟着状态走
watch(isRunning, (running) => {
  stopTick()
  if (running) {
    now.value = Date.now()
    tickTimer = window.setInterval(() => { now.value = Date.now() }, 1000)
  }
}, { immediate: true })
onBeforeUnmount(stopTick)

function parseTime(value?: string | null): number {
  if (!value) return Number.NaN
  const parsed = new Date(value).getTime()
  return Number.isFinite(parsed) ? parsed : Number.NaN
}

const startedTs = computed(() => {
  const own = parseTime(props.member.started_at)
  if (Number.isFinite(own)) return own
  const team = parseTime(props.teamStartedAt)
  if (Number.isFinite(team)) return team
  const firstEvent = sortedEvents.value[0]
  const eventTs = parseTime(firstEvent?.created_at)
  return Number.isFinite(eventTs) ? eventTs : Number.NaN
})

const elapsedSeconds = computed(() => {
  if (!Number.isFinite(startedTs.value)) return 0
  const end = props.member.status === 'completed' || props.member.status === 'failed'
    ? parseTime(props.member.completed_at)
    : now.value
  if (!Number.isFinite(end)) return 0
  return Math.max(0, Math.floor((end - startedTs.value) / 1000))
})

/** Codex「1m 41s」的中文形态:42秒 / 1分41秒 / 1小时5分。 */
function formatElapsed(totalSeconds: number): string {
  if (totalSeconds < 60) return `${totalSeconds}秒`
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (minutes < 60) return seconds ? `${minutes}分${seconds}秒` : `${minutes}分`
  const hours = Math.floor(minutes / 60)
  const restMinutes = minutes % 60
  return restMinutes ? `${hours}小时${restMinutes}分` : `${hours}小时`
}

const hasElapsed = computed(() => elapsedSeconds.value > 0 && Number.isFinite(startedTs.value))

/* ── 状态行:正在做的事 / 思考中 / 终态 ────────────────── */
const memberTasks = computed(() => props.tasks.filter((task) => (
  task.member_id === props.member.member_id
  || (task.member_key && task.member_key === props.member.member_key)
)))

const runningTask = computed(() => memberTasks.value.find((task) => task.status === 'running'))

/** 状态主文案:running 时优先展示当前任务标题,否则为通俗状态词。 */
const statusText = computed(() => {
  switch (props.member.status) {
    case 'created':
    case 'queued':
      return '排队等待派活'
    case 'running':
      return runningTask.value ? `正在:${runningTask.value.title}` : '思考中…'
    case 'completed':
      return '已完成'
    case 'failed':
      return '出错了'
    case 'reclaimed':
      return '已回收'
    default:
      return props.member.status
  }
})

/** 状态后缀:运行中是实时计时,终态是总耗时。 */
const timingText = computed(() => {
  if (!hasElapsed.value) return ''
  if (isRunning.value) return `已工作 ${formatElapsed(elapsedSeconds.value)}`
  if (props.member.status === 'completed') return `用时 ${formatElapsed(elapsedSeconds.value)}`
  return ''
})

/* ── 等宽操作日志:成员最近事件,带时间戳 ──────────────── */
const sortedEvents = computed(() => {
  const list = props.events.filter((event) => event.member_id === props.member.member_id)
  return list.sort((left, right) => {
    const leftTs = parseTime(left.created_at) || left.event_id
    const rightTs = parseTime(right.created_at) || right.event_id
    return (Number.isFinite(leftTs) ? leftTs : 0) - (Number.isFinite(rightTs) ? rightTs : 0)
  })
})

interface LogLine {
  key: string
  time: string
  text: string
}

function formatClockTime(value?: string | null): string {
  const ts = parseTime(value)
  if (!Number.isFinite(ts)) return ''
  const date = new Date(ts)
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

const logLines = computed<LogLine[]>(() => {
  const lines: LogLine[] = []
  for (const event of sortedEvents.value) {
    const action = EVENT_ACTION_TEXT[event.event_type]
    if (!action) continue
    const taskKey = typeof event.detail?.task_key === 'string' ? event.detail.task_key : ''
    lines.push({
      key: `event-${event.event_id}`,
      time: formatClockTime(event.created_at),
      text: taskKey ? `${action} ${taskKey}` : action,
    })
  }
  // 事件缺失时兜底:进行中/排队任务标题(最多3条)+完成数摘要,不泄露长任务列表全量标题
  if (!lines.length) {
    const active = memberTasks.value.filter((task) => task.status === 'running' || task.status === 'queued')
    for (const task of active.slice(0, 3)) {
      lines.push({ key: `task-${task.task_id}`, time: '', text: `${task.status === 'running' ? '进行中' : '排队'} ${task.title}` })
    }
    const doneCount = memberTasks.value.filter((task) => task.status === 'completed').length
    if (doneCount && lines.length < props.logLimit) {
      lines.push({ key: 'task-done-summary', time: '', text: `已完成 ${doneCount} 项任务` })
    }
  }
  return lines.slice(-props.logLimit)
})

function statusClass(status: string): string {
  return `is-${status.replace(/[^a-z0-9_-]/gi, '-')}`
}
</script>

<template>
  <article class="member-work-card" :class="statusClass(member.status)" aria-label="子Agent工作卡片">
    <span class="member-work-avatar" :class="`is-${theme}`" aria-hidden="true">{{ initial }}</span>
    <div class="member-work-main">
      <div class="member-work-head">
        <strong class="member-work-name">{{ member.display_name }}</strong>
        <span class="member-work-role" :class="`is-${theme}`">{{ roleLabel(member.role) }}</span>
        <button
          v-if="showAsk"
          class="member-work-ask team-window-ask"
          type="button"
          title="在聊天中追问该成员"
          @click.stop="emit('ask', member)"
        >追问</button>
      </div>
      <div class="member-work-status" :class="statusClass(member.status)">
        <i v-if="isRunning" class="member-work-pulse" aria-hidden="true"></i>
        <span class="member-work-status-text" :title="statusText">{{ statusText }}</span>
        <span v-if="timingText" class="member-work-timing">{{ timingText }}</span>
      </div>
      <ul v-if="logLines.length" class="member-work-log" aria-label="最近操作">
        <li v-for="line in logLines" :key="line.key">
          <time v-if="line.time">{{ line.time }}</time>
          <span class="member-work-log-text">{{ line.text }}</span>
        </li>
      </ul>
    </div>
  </article>
</template>

<style scoped>
.member-work-card {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--gray-200, #e0e3ea);
  border-radius: var(--r-md, 10px);
  background: var(--gray-50, #f7f8fa);
}
.member-work-card.is-running {
  border-color: var(--brand-200, #b7b3fb);
  background: var(--brand-50, #EFEEFE);
}
.member-work-card.is-failed { border-color: rgba(220, 73, 97, 0.35); }
.member-work-card.is-completed { border-color: var(--color-success-light, #e5f4ec); }

.member-work-avatar {
  display: grid;
  place-items: center;
  flex: none;
  width: 30px;
  height: 30px;
  border-radius: 50%;
  color: #fff;
  font-size: 13px;
  font-weight: 650;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.18);
}
/* 角色配色:小菱/主持=品牌紫,执行=青,验证=金,汇总=绿 */
.member-work-avatar.is-manager { background: linear-gradient(135deg, var(--brand-500, #5b58e8), var(--brand-400, #6f69ee)); }
.member-work-avatar.is-worker { background: linear-gradient(135deg, var(--accent-500, #25a5c4), var(--accent-400, #3dbcd9)); }
.member-work-avatar.is-verifier { background: linear-gradient(135deg, var(--sev-medium, #d9a857), #e8c07a); }
.member-work-avatar.is-summarizer { background: linear-gradient(135deg, var(--color-success, #4fb87a), #7fce9b); }

.member-work-main { display: flex; flex-direction: column; gap: 3px; min-width: 0; flex: 1; }
.member-work-head { display: flex; align-items: center; gap: 6px; min-width: 0; }
.member-work-name {
  min-width: 0;
  overflow: hidden;
  color: var(--gray-900, #161a24);
  font-size: 13px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.member-work-role {
  flex: none;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--brand-50, #EFEEFE);
  color: var(--brand-600, #4a46d4);
  font-size: 10px;
  line-height: 16px;
}
.member-work-role.is-manager { background: var(--brand-50, #EFEEFE); color: var(--brand-600, #4a46d4); }
.member-work-role.is-worker { background: rgba(61, 188, 217, 0.12); color: var(--accent-600, #1c849e); }
.member-work-role.is-verifier { background: var(--sev-medium-bg, #faf1df); color: var(--sev-medium, #d9a857); }
.member-work-role.is-summarizer { background: var(--color-success-light, #e5f4ec); color: var(--color-success, #4fb87a); }

.member-work-ask {
  flex: none;
  margin-left: auto;
  padding: 3px 10px;
  min-height: 40px;
  border: 1px solid var(--brand-300, #8e88f5);
  border-radius: 999px;
  background: var(--brand-50, #EFEEFE);
  color: var(--brand-600, #4a46d4);
  font-size: 11px;
  cursor: pointer;
  transition: background 0.15s ease;
}
.member-work-ask:hover { background: var(--brand-100, #dcdafd); }

.member-work-status {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  color: var(--gray-500, #6e7689);
  font-size: 11.5px;
  line-height: 1.4;
}
.member-work-status.is-running { color: var(--brand-600, #4a46d4); }
.member-work-status.is-completed { color: var(--color-success, #4fb87a); }
.member-work-status.is-failed { color: var(--color-danger, #dc4961); }
.member-work-status-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.member-work-timing { flex: none; color: var(--gray-400, #9ba3b0); font-size: 10.5px; }
.member-work-card.is-completed .member-work-timing { color: var(--color-success, #4fb87a); opacity: 0.8; }

.member-work-pulse {
  flex: none;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
  animation: member-work-breathe 1.2s ease-in-out infinite;
}
@keyframes member-work-breathe { 0%, 100% { opacity: 0.35; transform: scale(0.85); } 50% { opacity: 1; transform: scale(1); } }

/* 等宽操作日志:时间戳灰 + 动作文字,Codex 的「Ran …」对应物 */
.member-work-log {
  display: grid;
  gap: 2px;
  margin: 2px 0 0;
  padding: 0;
  list-style: none;
}
.member-work-log li {
  display: flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
  color: var(--gray-500, #6e7689);
  font-family: var(--font-mono, 'JetBrains Mono', Menlo, monospace);
  font-size: 10.5px;
  line-height: 1.5;
}
.member-work-log time { flex: none; color: var(--gray-400, #9ba3b0); }
.member-work-log-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (prefers-reduced-motion: reduce) {
  .member-work-pulse { animation: none; }
  .member-work-ask { transition: none; }
}
</style>
