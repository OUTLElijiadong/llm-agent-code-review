<script setup lang="ts">
import { computed } from 'vue'

import type {
  AgentTeamEvent,
  AgentTeamMember,
  AgentTeamTask,
} from '@/api/agentTeams'

/**
 * Codex 风格「子 Agent 工作卡片」:一个子 Agent 一张卡。
 *
 * 结构参照被认可的 Codex 多 Agent 界面:
 * 展示成员身份、派发任务、状态和完成计数；不展示执行正文或事件详情。
 */
const props = withDefaults(defineProps<{
  member: AgentTeamMember
  tasks?: AgentTeamTask[]
  /** 保持 AgentTeamTrace 现有调用兼容；卡片不再渲染事件日志或计时。 */
  events?: AgentTeamEvent[]
  teamStartedAt?: string | null
  /** 是否展示「追问」按钮(团队悬浮窗内)。 */
  showAsk?: boolean
  logLimit?: number
  /** 是否展示「停止此任务」按钮(团队仍在运行时由父组件开启)。 */
  showStop?: boolean
}>(), {
  tasks: () => [],
  events: () => [],
  teamStartedAt: null,
  showAsk: false,
  logLimit: 3,
  showStop: false,
})

const emit = defineEmits<{ ask: [member: AgentTeamMember]; stop: [task: AgentTeamTask] }>()

const TASK_STATUS_TEXT: Record<string, string> = {
  waiting_dependency: '等待前置', queued: '排队', running: '进行中', completed: '已完成',
  failed: '失败', dead_letter: '失败', blocked: '被阻断', cancelled: '已停止', expired: '已过期',
}
const TERMINAL_TASK = new Set(['completed', 'failed', 'dead_letter', 'blocked', 'cancelled', 'expired'])

function taskStatusText(status: string): string {
  return TASK_STATUS_TEXT[status] ?? status
}

function canStopTask(task: AgentTeamTask): boolean {
  return props.showStop && !TERMINAL_TASK.has(task.status)
}

const ROLE_LABELS: Record<string, string> = {
  worker: '执行', verifier: '验证', summarizer: '汇总',
}

function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role
}

const theme = computed<'manager' | 'worker' | 'verifier' | 'summarizer'>(() => (
  ROLE_LABELS[props.member.role] ? props.member.role : 'manager'
))

const initial = computed(() => (props.member.display_name || '员').trim().charAt(0))
const isRunning = computed(() => props.member.status === 'running')

/* ── 状态行:正在做的事 / 思考中 / 终态 ────────────────── */
const memberTasks = computed(() => props.tasks.filter((task) => (
  task.member_id === props.member.member_id
  || (task.member_key && task.member_key === props.member.member_key)
)))

const runningTask = computed(() => memberTasks.value.find((task) => task.status === 'running'))

/**
 * 卡片只展开未完成的任务(进行中/排队/失败等)及其派发内容,最多 4 条;
 * 已完成任务折叠为日志里的「已完成 N 项」,完整列表在团队任务页查看。
 */
const VISIBLE_TASK_LIMIT = 4
const openTasks = computed(() => memberTasks.value.filter((task) => task.status !== 'completed'))
const visibleTasks = computed(() => openTasks.value.slice(0, VISIBLE_TASK_LIMIT))
const hiddenTaskCount = computed(() => Math.max(0, openTasks.value.length - VISIBLE_TASK_LIMIT))

/** 成员级进度:小菱派给它的任务完成了几项。 */
const memberProgressText = computed(() => {
  const total = memberTasks.value.length
  if (!total) return ''
  const done = memberTasks.value.filter((task) => task.status === 'completed').length
  return `完成 ${done}/${total}`
})

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
        <span v-if="member.kind === 'temporary'" class="member-work-role" title="仅服务当前账号的本次任务；结束后保留审计记录">本任务临时</span>
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
        <span v-if="memberProgressText" class="member-work-progress">{{ memberProgressText }}</span>
      </div>
      <ol v-if="visibleTasks.length" class="member-work-tasks" aria-label="小菱派给该成员的任务">
        <li v-for="task in visibleTasks" :key="task.task_id" class="member-work-task" :class="statusClass(task.status)">
          <div class="member-work-task-head">
            <span class="member-work-task-status">{{ taskStatusText(task.status) }}</span>
            <span class="member-work-task-title" :title="task.title">{{ task.title }}</span>
            <span v-if="task.attempt_count > 1" class="member-work-task-attempt">第 {{ task.attempt_count }} 次</span>
            <button
              v-if="canStopTask(task)"
              class="member-work-stop"
              type="button"
              :title="`只停止「${task.title}」，其余成员继续`"
              @click.stop="emit('stop', task)"
            >停止</button>
          </div>
        </li>
        <li v-if="hiddenTaskCount" class="member-work-task-more">另有 {{ hiddenTaskCount }} 项未完成任务，见团队任务列表</li>
      </ol>
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
.member-work-pulse {
  flex: none;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
  animation: member-work-breathe 1.2s ease-in-out infinite;
}
@keyframes member-work-breathe { 0%, 100% { opacity: 0.35; transform: scale(0.85); } 50% { opacity: 1; transform: scale(1); } }

@media (prefers-reduced-motion: reduce) {
  .member-work-pulse { animation: none; }
  .member-work-ask { transition: none; }
}
.member-work-progress {
  flex: none;
  margin-left: auto;
  color: var(--gray-500, #6b7280);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.member-work-tasks { display: flex; flex-direction: column; gap: 4px; margin: 4px 0 0; padding: 0; list-style: none; }
.member-work-task {
  padding: 5px 8px;
  border: 1px solid var(--gray-200, #e0e3ea);
  border-radius: 8px;
  background: #fff;
}
.member-work-task.is-running { border-color: var(--brand-200, #b7b3fb); }
.member-work-task.is-cancelled, .member-work-task.is-blocked { opacity: 0.72; }
.member-work-task-head { display: flex; align-items: center; gap: 6px; min-width: 0; font-size: 12px; }
.member-work-task-status { flex: none; color: var(--brand-600, #4f46e5); font-weight: 600; }
.member-work-task.is-completed .member-work-task-status { color: var(--color-success, #2f9e5f); }
.member-work-task.is-failed .member-work-task-status,
.member-work-task.is-dead_letter .member-work-task-status { color: var(--el-color-danger, #d03050); }
.member-work-task-title { min-width: 0; overflow: hidden; color: var(--gray-800, #1f2937); text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.member-work-task-attempt { flex: none; color: var(--gray-500, #6b7280); font-size: 11px; }
.member-work-stop {
  flex: none;
  padding: 1px 8px;
  border: 1px solid rgba(220, 73, 97, 0.45);
  border-radius: 999px;
  background: #fff;
  color: var(--el-color-danger, #d03050);
  font-size: 11px;
  cursor: pointer;
}
.member-work-stop:hover { background: rgba(220, 73, 97, 0.08); }
.member-work-stop:focus-visible { outline: 2px solid var(--el-color-danger, #d03050); outline-offset: 1px; }
.member-work-task-more { color: var(--gray-500, #6b7280); font-size: 11px; padding-left: 2px; }
</style>
