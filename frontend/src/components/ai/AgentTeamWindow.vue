<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { Close } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'

import {
  cancelAgentTeam,
  retryAgentTeam,
  type AgentTeamDetail,
  type AgentTeamMember,
  type AgentTeamMessage,
  type AgentTeamTask,
} from '@/api/agentTeams'
import { useFloatingChatPosition } from '@/composables/useFloatingChatPosition'

/**
 * 子 Agent 团队独立悬浮窗。
 *
 * 区别于主聊天面板(AgentChatDrawer):这是一个可拖拽的独立浮窗,
 * 默认展开地展示某个子 Agent 团队的完整工作内容——成员/任务/消息流,
 * 并提供「重试失败」「取消团队」操作入口。
 */

const props = defineProps<{
  visible: boolean
  team: AgentTeamDetail | null
}>()
const emit = defineEmits<{
  'update:visible': [value: boolean]
  /** 重试/取消成功后通知父组件刷新团队数据 */
  refreshed: []
}>()

const { panelRef, style: panelStyle, dragging, restoreOrAnchor, beginDrag, moveDrag, endDrag } =
  useFloatingChatPosition('agent-team-window')

const actionSubmitting = ref(false)

const members = computed<AgentTeamMember[]>(() => props.team?.members ?? [])
const tasks = computed<AgentTeamTask[]>(() => props.team?.tasks ?? [])
const messages = computed<AgentTeamMessage[]>(() =>
  [...(props.team?.messages ?? [])].sort((left, right) => {
    if (left.ledger_id !== undefined && right.ledger_id !== undefined) return left.ledger_id - right.ledger_id
    return 0
  }),
)

/** 进度统计:优先用后端 counts,缺失时按任务状态本地统计。 */
const counts = computed(() => {
  if (props.team?.counts) return props.team.counts
  const list = tasks.value
  return {
    total: list.length,
    completed: list.filter((task) => task.status === 'completed').length,
    running: list.filter((task) => task.status === 'running').length,
    queued: list.filter((task) => ['queued', 'waiting_dependency'].includes(task.status)).length,
    failed: list.filter((task) => ['failed', 'dead_letter', 'expired'].includes(task.status)).length,
    blocked: list.filter((task) => task.status === 'blocked').length,
  }
})

const progressPercent = computed(() => {
  const total = counts.value.total
  return total > 0 ? Math.round((counts.value.completed / total) * 100) : 0
})

/** 有失败/死信/过期任务时才允许「重试失败」。 */
const failedTaskKeys = computed(() =>
  tasks.value
    .filter((task) => ['failed', 'dead_letter', 'expired'].includes(task.status))
    .map((task) => task.task_key),
)
const canRetry = computed(() => failedTaskKeys.value.length > 0 && !actionSubmitting.value)
const canCancel = computed(() => {
  const status = props.team?.status
  return Boolean(status && ['draft', 'queued', 'running', 'verifying'].includes(status)) && !actionSubmitting.value
})

const TEAM_STATUS_LABELS: Record<string, string> = {
  draft: '草稿', queued: '排队中', running: '运行中', verifying: '验证中',
  completed: '已完成', failed: '失败', cancelled: '已取消', expired: '已过期',
}
const TASK_STATUS_LABELS: Record<string, string> = {
  waiting_dependency: '等待依赖', queued: '排队中', running: '运行中',
  completed: '已完成', failed: '失败', blocked: '已阻塞',
  cancelled: '已取消', dead_letter: '死信', expired: '已过期',
}
const MEMBER_STATUS_LABELS: Record<string, string> = {
  created: '已创建', queued: '排队中', running: '运行中',
  completed: '已完成', failed: '失败', reclaimed: '已回收',
}
const ROLE_LABELS: Record<string, string> = {
  worker: '执行', verifier: '验证', summarizer: '汇总',
}

function teamStatusLabel(status: string): string {
  return TEAM_STATUS_LABELS[status] ?? status
}
function taskStatusLabel(status: string): string {
  return TASK_STATUS_LABELS[status] ?? status
}
function memberStatusLabel(status: string): string {
  return MEMBER_STATUS_LABELS[status] ?? status
}
function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role
}
function statusClass(status: string): string {
  return `is-${status.replace(/[^a-z0-9_-]/gi, '-')}`
}

function taskMember(task: AgentTeamTask): string {
  return task.member_key || members.value.find((member) => member.member_id === task.member_id)?.display_name || '未分配'
}

function formatTime(value?: string | null): string {
  if (!value) return ''
  const timestamp = new Date(value)
  if (Number.isNaN(timestamp.getTime())) return ''
  return timestamp.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
}

function formatFullDetail(value: unknown): string {
  if (value === undefined || value === null) return ''
  try {
    return typeof value === 'string' ? value : JSON.stringify(value) || ''
  } catch {
    return String(value)
  }
}

function summarize(value: unknown, limit = 160): string {
  const text = formatFullDetail(value)
  return text.length > limit ? `${text.slice(0, limit)}...` : text
}

function close(): void {
  emit('update:visible', false)
}

watch(
  () => props.visible,
  async (val) => {
    if (!val) return
    // 等待浮窗渲染完成后再恢复/锚定位置
    await nextTick()
    restoreOrAnchor()
  },
)

async function retryFailed(): Promise<void> {
  if (!props.team || !canRetry.value) return
  try {
    await ElMessageBox.confirm(
      `将重试 ${failedTaskKeys.value.length} 个失败任务,其余已完成任务不会重跑。要继续吗?`,
      '重试失败任务',
      { confirmButtonText: '重试', cancelButtonText: '先不了', type: 'warning' },
    )
  } catch {
    return
  }
  actionSubmitting.value = true
  try {
    await retryAgentTeam(props.team.team_id, failedTaskKeys.value)
    ElMessage.success('已发起重试,团队会继续执行失败任务')
    emit('refreshed')
  } catch {
    ElMessage.error('重试发起失败,请稍后再试')
  } finally {
    actionSubmitting.value = false
  }
}

async function cancelTeam(): Promise<void> {
  if (!props.team || !canCancel.value) return
  try {
    await ElMessageBox.confirm(
      '取消后团队内未完成的任务都会停止,该操作不可撤销。确定取消这个团队吗?',
      '取消团队',
      { confirmButtonText: '取消团队', cancelButtonText: '先不了', type: 'warning', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }
  actionSubmitting.value = true
  try {
    await cancelAgentTeam(props.team.team_id, '用户在悬浮窗手动取消')
    ElMessage.success('团队已取消')
    emit('refreshed')
  } catch {
    ElMessage.error('取消失败,请稍后再试')
  } finally {
    actionSubmitting.value = false
  }
}
</script>

<template>
  <Teleport to="body">
    <Transition name="team-window">
      <div
        v-if="visible && team"
        ref="panelRef"
        class="agent-team-window"
        :class="{ 'is-dragging': dragging }"
        :style="panelStyle"
        role="dialog"
        aria-label="子 Agent 团队详情"
        @pointermove="moveDrag"
        @pointerup="endDrag"
        @pointercancel="endDrag"
      >
        <header class="team-window-header">
          <button
            class="team-window-drag"
            type="button"
            aria-label="移动团队详情窗口"
            title="拖拽移动窗口"
            @pointerdown="beginDrag"
          >⠿</button>
          <div class="team-window-title">
            <strong>{{ team.title }}</strong>
            <span class="team-window-status" :class="statusClass(team.status)">{{ teamStatusLabel(team.status) }}</span>
          </div>
          <button
            class="team-window-close"
            type="button"
            aria-label="关闭团队详情窗口"
            title="关闭"
            @click="close"
          >
            <el-icon><Close /></el-icon>
          </button>
        </header>

        <div class="team-window-progress" aria-label="团队进度">
          <div class="team-window-progress-bar">
            <i :style="{ width: `${progressPercent}%` }"></i>
          </div>
          <div class="team-window-stats">
            <span><b>{{ counts.completed }}</b>完成</span>
            <span><b>{{ counts.running }}</b>运行</span>
            <span><b>{{ counts.queued }}</b>排队</span>
            <span><b>{{ counts.failed + counts.blocked }}</b>失败/阻塞</span>
            <span class="team-window-total">共 {{ counts.total }} 项</span>
          </div>
        </div>

        <div class="team-window-body">
          <section v-if="members.length" class="team-window-section" aria-label="团队成员">
            <h4>成员 · {{ members.length }}</h4>
            <ul class="team-window-members">
              <li v-for="member in members" :key="member.member_id">
                <span class="team-window-dot" :class="statusClass(member.status)" aria-hidden="true"></span>
                <span class="team-window-member-name">{{ member.display_name }}</span>
                <span class="team-window-role">{{ roleLabel(member.role) }}</span>
                <span class="team-window-item-status">{{ memberStatusLabel(member.status) }}</span>
              </li>
            </ul>
          </section>

          <section v-if="tasks.length" class="team-window-section" aria-label="团队任务">
            <h4>任务 · {{ tasks.length }}</h4>
            <ol class="team-window-tasks">
              <li v-for="task in tasks" :key="task.task_id" :class="statusClass(task.status)">
                <div class="team-window-task-line">
                  <strong>{{ task.title }}</strong>
                  <span class="team-window-item-status">{{ taskStatusLabel(task.status) }}</span>
                </div>
                <small>
                  {{ taskMember(task) }} · 第 {{ task.attempt_count }}/{{ task.max_attempts }} 次尝试
                  <template v-if="task.depends_on.length"> · 依赖 {{ task.depends_on.join('、') }}</template>
                </small>
                <details v-if="task.errors?.length" class="team-window-task-error">
                  <summary>错误信息</summary>
                  <code>{{ formatFullDetail(task.errors) }}</code>
                </details>
              </li>
            </ol>
          </section>

          <section v-if="messages.length" class="team-window-section" aria-label="协作消息流">
            <h4>协作消息 · {{ messages.length }}</h4>
            <ul class="team-window-messages">
              <li v-for="message in messages" :key="message.message_id">
                <div class="team-window-message-line">
                  <span class="team-window-message-route">{{ message.sent_from }} → {{ message.send_to }}</span>
                  <time>{{ formatTime(message.create_time ?? message.created_at) }}</time>
                </div>
                <p class="team-window-message-subject">{{ message.subject || message.message_type }}</p>
                <p v-if="message.payload" class="team-window-message-payload">{{ summarize(message.payload) }}</p>
              </li>
            </ul>
          </section>

          <p v-if="!members.length && !tasks.length && !messages.length" class="team-window-empty">
            团队详情正在同步…
          </p>
        </div>

        <footer class="team-window-footer">
          <button
            class="team-window-action is-retry"
            type="button"
            :disabled="!canRetry"
            title="只重跑失败/死信/过期的任务"
            @click="retryFailed"
          >重试失败</button>
          <button
            class="team-window-action is-cancel"
            type="button"
            :disabled="!canCancel"
            @click="cancelTeam"
          >取消团队</button>
        </footer>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.agent-team-window {
  position: fixed;
  right: 24px;
  bottom: 24px;
  z-index: var(--z-index-popover);
  display: flex;
  flex-direction: column;
  width: min(440px, calc(100vw - 24px));
  max-height: min(600px, calc(100vh - 48px));
  overflow: hidden;
  border: 1px solid var(--color-border-base);
  border-radius: var(--r-lg);
  background: var(--color-bg-card);
  box-shadow: var(--shadow-3);
  color: var(--color-text-primary);
  font-size: var(--fs-xs);
}
.agent-team-window.is-dragging { user-select: none; }

.team-window-header {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--color-border-light);
  background: linear-gradient(90deg, rgba(91, 88, 232, 0.07), rgba(61, 188, 217, 0.07));
}
.team-window-drag {
  display: grid;
  place-items: center;
  flex: none;
  width: 22px;
  height: 28px;
  border: 0;
  border-radius: var(--r-sm);
  background: transparent;
  color: var(--color-text-placeholder);
  font-size: 16px;
  cursor: grab;
  touch-action: none;
}
.team-window-drag:hover { color: var(--brand-500); background: var(--brand-50); }
.team-window-title {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  min-width: 0;
  flex: 1;
}
.team-window-title strong { min-width: 0; overflow-wrap: anywhere; font-size: 13px; }
.team-window-status {
  flex: none;
  padding: 1px 8px;
  border: 1px solid var(--color-border-base);
  border-radius: 999px;
  color: var(--color-text-secondary);
  font-size: 10px;
  white-space: nowrap;
}
.team-window-status.is-running,
.team-window-status.is-verifying { color: var(--brand-600); border-color: var(--brand-200); background: var(--brand-50); }
.team-window-status.is-completed { color: var(--color-success); border-color: var(--color-success); background: var(--color-success-light); }
.team-window-status.is-failed,
.team-window-status.is-expired { color: var(--color-danger); border-color: var(--color-danger); }
.team-window-close {
  display: grid;
  place-items: center;
  flex: none;
  width: 26px;
  height: 26px;
  border: 0;
  border-radius: var(--r-sm);
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
}
.team-window-close:hover { color: var(--color-text-primary); background: var(--gray-50); }

.team-window-progress {
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--color-border-light);
}
.team-window-progress-bar {
  height: 5px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--gray-50);
}
.team-window-progress-bar i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, var(--brand-400), var(--accent-400));
  transition: width 0.3s ease;
}
.team-window-stats {
  display: flex;
  align-items: baseline;
  gap: var(--sp-3);
  margin-top: var(--sp-1);
  color: var(--color-text-secondary);
}
.team-window-stats b { color: var(--color-text-primary); font-size: 13px; margin-right: 2px; }
.team-window-total { margin-left: auto; color: var(--color-text-placeholder); }

.team-window-body {
  min-height: 0;
  flex: 1;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: var(--sp-2) var(--sp-3);
}
.team-window-section { min-width: 0; padding-top: var(--sp-2); }
.team-window-section:first-child { padding-top: 0; }
.team-window-section h4 { margin: 0 0 5px; color: var(--color-text-secondary); font-size: 11px; font-weight: 650; }

.team-window-members,
.team-window-tasks,
.team-window-messages { display: grid; gap: 4px; margin: 0; padding: 0; list-style: none; }

.team-window-members li {
  display: grid;
  grid-template-columns: 8px minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 6px;
  min-width: 0;
  padding: 5px 6px;
  border: 1px solid var(--color-border-light);
  border-radius: var(--r-sm);
}
.team-window-member-name { min-width: 0; overflow-wrap: anywhere; font-weight: 550; }
.team-window-role {
  flex: none;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--brand-50);
  color: var(--brand-600);
  font-size: 10px;
}
.team-window-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--gray-400); }
.team-window-dot.is-running { background: var(--color-info); }
.team-window-dot.is-completed { background: var(--color-success); }
.team-window-dot.is-failed { background: var(--color-danger); }

.team-window-tasks li {
  min-width: 0;
  padding: 6px 8px;
  border: 1px solid var(--color-border-light);
  border-left: 3px solid var(--color-info);
  border-radius: var(--r-sm);
}
.team-window-tasks li.is-completed { border-left-color: var(--color-success); }
.team-window-tasks li.is-failed,
.team-window-tasks li.is-dead_letter,
.team-window-tasks li.is-expired { border-left-color: var(--color-danger); }
.team-window-tasks li.is-blocked,
.team-window-tasks li.is-waiting_dependency { border-left-color: var(--color-warning); }
.team-window-task-line { display: flex; align-items: baseline; justify-content: space-between; gap: 6px; min-width: 0; }
.team-window-task-line strong { min-width: 0; overflow-wrap: anywhere; }
.team-window-tasks small { display: block; margin-top: 2px; color: var(--color-text-placeholder); overflow-wrap: anywhere; }
.team-window-task-error { margin-top: 4px; color: var(--color-text-secondary); }
.team-window-task-error summary { cursor: pointer; color: var(--color-danger); font-size: 10px; }
.team-window-task-error code { display: block; margin-top: 4px; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 10px; }

.team-window-item-status { flex: none; color: var(--color-text-secondary); font-size: 10px; white-space: nowrap; }

.team-window-messages li {
  min-width: 0;
  padding: 5px 0;
  border-bottom: 1px solid var(--color-border-light);
}
.team-window-message-line { display: flex; align-items: baseline; justify-content: space-between; gap: 6px; min-width: 0; }
.team-window-message-route { min-width: 0; overflow-wrap: anywhere; color: var(--brand-600); font-weight: 550; }
.team-window-message-line time { flex: none; color: var(--color-text-placeholder); font-size: 10px; }
.team-window-message-subject { margin: 2px 0 0; overflow-wrap: anywhere; }
.team-window-message-payload { margin: 2px 0 0; color: var(--color-text-secondary); overflow-wrap: anywhere; }

.team-window-empty { margin: var(--sp-3) 0; color: var(--color-text-placeholder); text-align: center; }

.team-window-footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-top: 1px solid var(--color-border-light);
}
.team-window-action {
  min-width: 76px;
  min-height: 28px;
  border: 1px solid var(--color-border-base);
  border-radius: var(--r-sm);
  background: var(--color-bg-card);
  color: var(--color-text-primary);
  font-size: var(--fs-xs);
  cursor: pointer;
  transition: all var(--transition-fast);
}
.team-window-action:hover:not(:disabled) { border-color: var(--brand-300); color: var(--brand-600); }
.team-window-action:disabled { opacity: 0.5; cursor: not-allowed; }
.team-window-action.is-retry:not(:disabled) { border-color: var(--brand-300); color: var(--brand-600); background: var(--brand-50); }
.team-window-action.is-cancel:not(:disabled):hover { border-color: var(--color-danger); color: var(--color-danger); }

.team-window-enter-active,
.team-window-leave-active { transition: opacity 0.2s ease, transform 0.2s ease; }
.team-window-enter-from,
.team-window-leave-to { opacity: 0; transform: translateY(12px) scale(0.98); }

@media (max-width: 520px) {
  .agent-team-window { right: 12px; bottom: 12px; left: 12px; width: auto; }
}

@media (prefers-reduced-motion: reduce) {
  .team-window-progress-bar i,
  .team-window-action,
  .team-window-enter-active,
  .team-window-leave-active { transition: none; }
}
</style>
