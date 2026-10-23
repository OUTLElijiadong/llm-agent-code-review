<script setup lang="ts">
import { computed, h, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Close } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'

import {
  cancelAgentTeam,
  cancelAgentTeamTasks,
  previewRetryAgentTeam,
  retryAgentTeam,
  type AgentTeamDetail,
  type AgentTeamMember,
  type AgentTeamTask,
} from '@/api/agentTeams'
import AgentMemberWorkCard from '@/components/ai/AgentMemberWorkCard.vue'
import { useFloatingChatPosition } from '@/composables/useFloatingChatPosition'
import { useUserStore } from '@/stores/user'

/**
 * 子 Agent 团队独立悬浮窗。
 *
 * 区别于主聊天面板(AgentChatDrawer):这是一个可拖拽的独立浮窗,
 * 只展示团队状态、成员任务分派和进度，不展示结论或协作正文。
 * 提供「重试失败」「取消团队」操作入口。
 */

const props = defineProps<{
  visible: boolean
  team: AgentTeamDetail | null
}>()
const emit = defineEmits<{
  'update:visible': [value: boolean]
  /** 重试/取消成功后通知父组件刷新团队数据 */
  refreshed: []
  /** 在聊天中追问某成员:父组件预填输入框,支持做完后修正。 */
  'ask-member': [payload: { teamId: number; name: string; address: string }]
}>()

const { panelRef, style: panelStyle, dragging, restoreOrAnchor, beginDrag, moveDrag, endDrag } =
  useFloatingChatPosition('agent-team-window')

const actionSubmitting = ref(false)
const userStore = useUserStore()
let targetGeneration = 0
let disposed = false

const members = computed<AgentTeamMember[]>(() => props.team?.members ?? [])
const tasks = computed<AgentTeamTask[]>(() => props.team?.tasks ?? [])

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
/** 团队仍在推进(运行/验证/排队)时进度条加流光动画。 */
const teamActive = computed(() => {
  const status = props.team?.status
  return Boolean(status && ['running', 'verifying', 'queued'].includes(status))
})

/** 有失败/死信/过期任务时才允许「重试失败」；整队阻塞时，补齐条件后可从阻塞根任务重试。 */
const CASCADE_BLOCK_CODES = new Set(['team_failed', 'dependency_failed'])
const failedTaskKeys = computed(() => {
  const failed = tasks.value.filter((task) => ['failed', 'dead_letter', 'expired'].includes(task.status))
  const roots = failed.length || props.team?.status !== 'blocked'
    ? failed
    : tasks.value.filter((task) => task.status === 'blocked'
      && !(task.errors ?? []).some((item) => CASCADE_BLOCK_CODES.has(String(item.code ?? ''))))
  return roots.map((task) => task.task_key)
})
const canRetry = computed(() => failedTaskKeys.value.length > 0 && userStore.profile?.id != null && !actionSubmitting.value)
const canCancel = computed(() => {
  const status = props.team?.status
  return Boolean(status && ['draft', 'queued', 'running', 'verifying'].includes(status)) && !actionSubmitting.value
})

watch(
  [() => props.team?.team_id, () => props.team?.session_id, () => props.visible, () => userStore.profile?.id, () => userStore.token],
  () => {
    targetGeneration += 1
    actionSubmitting.value = false
  },
  { flush: 'sync' },
)

const TEAM_STATUS_LABELS: Record<string, string> = {
  draft: '草稿', queued: '排队中', running: '运行中', verifying: '验证中',
  completed: '已完成', failed: '失败', blocked: '已阻塞', cancelled: '已取消', expired: '已过期',
}
const TASK_STATUS_LABELS: Record<string, string> = {
  waiting_dependency: '等待依赖', queued: '排队中', running: '运行中',
  completed: '已完成', failed: '失败', blocked: '已阻塞',
  cancelled: '已取消', dead_letter: '失败终止', expired: '已过期',
}
function teamStatusLabel(status: string): string {
  return TEAM_STATUS_LABELS[status] ?? status
}
function taskStatusLabel(status: string): string {
  return TASK_STATUS_LABELS[status] ?? status
}
function statusClass(status: string): string {
  return `is-${status.replace(/[^a-z0-9_-]/gi, '-')}`
}

function taskMember(task: AgentTeamTask): string {
  const member = members.value.find((item) => item.member_id === task.member_id || item.member_key === task.member_key)
  return member?.display_name || task.member_key || '未分配'
}

function close(): void {
  emit('update:visible', false)
}

interface TeamOperationTarget {
  generation: number
  teamId: number
  sessionId: string
  accountId: number
  token: string
}

/** 在弹出确认框前冻结团队、会话和身份，避免异步等待后操作漂移。 */
function captureTeamOperationTarget(): TeamOperationTarget | null {
  const team = props.team
  const accountId = userStore.profile?.id
  if (disposed || !props.visible || !team || accountId == null) return null
  return {
    generation: targetGeneration,
    teamId: team.team_id,
    sessionId: team.session_id,
    accountId,
    token: userStore.token,
  }
}

function isTeamOperationTargetCurrent(target: TeamOperationTarget): boolean {
  return Boolean(
    !disposed && props.visible
    && target.generation === targetGeneration
    && props.team?.team_id === target.teamId
    && props.team.session_id === target.sessionId
    && userStore.profile?.id === target.accountId
    && userStore.token === target.token,
  )
}

function notifyChangedTarget(): void {
  if (disposed) return
  ElMessage.error('团队、会话或账号已变化，本次操作已取消，请重新核对目标。')
}

watch(
  () => props.visible,
  async (val) => {
    if (!val) return
    // 等待浮窗渲染完成后再恢复/锚定位置
    await nextTick()
    if (disposed || !props.visible) return
    restoreOrAnchor()
  },
)

onBeforeUnmount(() => {
  disposed = true
  targetGeneration += 1
  actionSubmitting.value = false
})

async function retryFailed(): Promise<void> {
  const target = captureTeamOperationTarget()
  const taskKeys = [...failedTaskKeys.value]
  if (!target || !taskKeys.length || actionSubmitting.value) return
  const selectedTasks = tasks.value.filter((task) => taskKeys.includes(task.task_key))
  actionSubmitting.value = true
  try {
    let value: string
    try {
      const prompted = await ElMessageBox.prompt(
        `以下任务将作为本次重试起点：\n${selectedTasks.map((task) => `· ${task.title}（${task.task_key}）`).join('\n')}\n\n因依赖变化，后续阻断或已完成任务也可能重置；下一步会显示完整影响范围。`,
        '预览失败任务重试',
        {
          confirmButtonText: '生成预览',
          cancelButtonText: '先不了',
          inputType: 'textarea',
          inputPlaceholder: '例如：先按模块拆分，再逐段复核失败证据和边界条件',
          inputValidator: (raw: string) => {
            const strategy = raw.trim()
            if ([...strategy].length < 8) return '请填写至少 8 个字符的新执行方案'
            if (selectedTasks.some((task) => task.instructions?.includes(strategy))) return '新方案不能与原执行指令重复'
            return true
          },
        },
      )
      value = prompted.value
    } catch {
      return
    }
    if (!isTeamOperationTargetCurrent(target)) {
      notifyChangedTarget()
      return
    }
    const strategy = value.trim()
    if ([...strategy].length < 8 || selectedTasks.some((task) => task.instructions?.includes(strategy))) {
      ElMessage.error('新方案无效或与原执行指令重复，请重新核对后再试')
      return
    }
    const strategyChanges = Object.fromEntries(taskKeys.map((taskKey) => [taskKey, strategy]))
    const preview = await previewRetryAgentTeam(target.teamId, taskKeys, strategyChanges)
    if (!isTeamOperationTargetCurrent(target)) {
      notifyChangedTarget()
      return
    }
    const riskName: Record<string, string> = {
      low: '低', medium: '中', high: '高', critical: '危急',
    }
    const taskStatusName: Record<string, string> = {
      queued: '待执行', waiting_dependency: '等待依赖', running: '执行中', completed: '已完成',
      failed: '失败', blocked: '已阻断', expired: '已过期', dead_letter: '失败终止', cancelled: '已取消',
    }
    const confirmationTasks = preview.tasks.map((task) => {
      const risk = riskName[task.risk_level] ?? '未分类'
      const status = taskStatusName[task.status] ?? task.status
      const dependencies = task.depends_on.length ? `；依赖：${task.depends_on.join('、')}` : ''
      const marker = task.needs_confirmation ? '（需监督确认）' : ''
      return `· ${task.title}（${task.task_key}；当前${status}${dependencies}；${risk}风险${marker}）：${task.reason}`
    })
    try {
      await ElMessageBox.confirm(
        h('div', { style: {
          whiteSpace: 'pre-line',
          lineHeight: '1.65',
          maxHeight: '50vh',
          overflowY: 'auto',
          paddingRight: '8px',
        } }, [
          `本次重试将影响 ${preview.tasks.length} 个任务，最高风险为${riskName[preview.risk_level] ?? '未分类'}。`,
          '只会授权下面列出的本次任务和执行策略；任务状态或策略变化后必须重新预览。',
          ...confirmationTasks,
        ].join('\n\n')),
        preview.requires_confirmation ? '确认高风险重试' : '确认重试',
        {
          confirmButtonText: preview.requires_confirmation ? '确认本次高风险重试' : '确认本次重试',
          cancelButtonText: '取消',
          type: preview.requires_confirmation ? 'warning' : 'info',
          confirmButtonClass: preview.requires_confirmation ? 'el-button--danger' : '',
        },
      )
    } catch {
      return
    }
    if (!isTeamOperationTargetCurrent(target)) {
      notifyChangedTarget()
      return
    }
    await retryAgentTeam(target.teamId, taskKeys, strategyChanges, preview.plan_sha256)
    if (!isTeamOperationTargetCurrent(target)) return
    ElMessage.success('已发起重试,团队会继续执行失败任务')
    emit('refreshed')
  } catch {
    if (!isTeamOperationTargetCurrent(target)) return
    ElMessage.error('重试发起失败,请稍后再试')
  } finally {
    if (isTeamOperationTargetCurrent(target)) actionSubmitting.value = false
  }
}

/** 只停止一个方向错误的成员任务；其余成员继续，可再让小菱派发修正后的任务。 */
async function stopMemberTask(task: AgentTeamTask): Promise<void> {
  const target = captureTeamOperationTarget()
  if (!target || !canCancel.value) return
  actionSubmitting.value = true
  try {
    try {
      await ElMessageBox.confirm(
        `只停止「${task.title}」,其他成员继续执行;依赖它的后续任务会一并停止。需要改派时直接告诉小菱新的要求。`,
        '停止该成员任务',
        { confirmButtonText: '停止该任务', cancelButtonText: '先不了', type: 'warning', confirmButtonClass: 'el-button--danger' },
      )
    } catch {
      return
    }
    if (!isTeamOperationTargetCurrent(target)) {
      notifyChangedTarget()
      return
    }
    await cancelAgentTeamTasks(target.teamId, [task.task_key], '用户在团队悬浮窗停止该成员任务')
    if (!isTeamOperationTargetCurrent(target)) return
    ElMessage.success('已停止该成员任务,其余成员继续执行')
    emit('refreshed')
  } catch {
    if (!isTeamOperationTargetCurrent(target)) return
    ElMessage.error('停止失败,请稍后再试')
  } finally {
    if (isTeamOperationTargetCurrent(target)) actionSubmitting.value = false
  }
}

async function cancelTeam(): Promise<void> {
  const target = captureTeamOperationTarget()
  if (!target || !canCancel.value) return
  actionSubmitting.value = true
  try {
    try {
      await ElMessageBox.confirm(
        '取消后团队内未完成的任务都会停止,该操作不可撤销。确定取消这个团队吗?',
        '取消团队',
        { confirmButtonText: '取消团队', cancelButtonText: '先不了', type: 'warning', confirmButtonClass: 'el-button--danger' },
      )
    } catch {
      return
    }
    if (!isTeamOperationTargetCurrent(target)) {
      notifyChangedTarget()
      return
    }
    await cancelAgentTeam(target.teamId, '用户在悬浮窗手动取消')
    if (!isTeamOperationTargetCurrent(target)) return
    ElMessage.success('团队已取消')
    emit('refreshed')
  } catch {
    if (!isTeamOperationTargetCurrent(target)) return
    ElMessage.error('取消失败,请稍后再试')
  } finally {
    if (isTeamOperationTargetCurrent(target)) actionSubmitting.value = false
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

        <!-- 强化进度条:百分比 + 完成/总数 + 渐变流光 -->
        <div class="team-window-progress" aria-label="团队进度">
          <div class="team-window-progress-top">
            <span class="team-window-progress-pct">{{ progressPercent }}%</span>
            <span class="team-window-progress-frac">已完成 {{ counts.completed }}/{{ counts.total }} 项</span>
            <span class="team-window-progress-mini">
              <template v-if="counts.running">运行 {{ counts.running }}</template>
              <template v-if="counts.queued"> · 排队 {{ counts.queued }}</template>
              <template v-if="counts.failed + counts.blocked"> · 失败/阻塞 {{ counts.failed + counts.blocked }}</template>
            </span>
          </div>
          <div class="team-window-progress-bar" :class="{ 'is-active': teamActive && progressPercent < 100 }">
            <i :style="{ width: `${progressPercent}%` }"></i>
          </div>
        </div>

        <div class="team-window-body">
          <!-- 成员实时工作卡片 -->
          <section v-if="members.length" class="team-window-section team-window-members-section" aria-label="团队成员实时状态">
            <AgentMemberWorkCard
              v-for="member in members"
              :key="member.member_id"
              :member="member"
              :tasks="tasks"
              show-ask
              :show-stop="canCancel && !actionSubmitting"
              @ask="team && emit('ask-member', { teamId: team.team_id, name: $event.display_name, address: $event.address })"
              @stop="stopMemberTask"
            />
          </section>

          <p v-if="!members.length && !tasks.length" class="team-window-empty">
            团队详情正在同步…
          </p>

          <!-- 任务明细默认折叠,避免浮窗过长 -->
          <details v-if="tasks.length" class="team-window-task-details">
            <summary>任务明细 · {{ tasks.length }}</summary>
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
              </li>
            </ol>
          </details>
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
  /* 必须盖过聊天抽屉(3000)与 Element 弹层,否则被压在主聊天窗下面 */
  z-index: var(--layer-agent-team);
  display: flex;
  flex-direction: column;
  width: min(440px, calc(100vw - 24px));
  max-height: min(600px, calc(100dvh - 48px));
  overflow: hidden;
  border: 1px solid var(--color-border-base);
  border-radius: var(--r-lg);
  background: var(--color-bg-card);
  box-shadow: var(--shadow-3);
  color: var(--color-text-primary);
  font-size: var(--fs-xs);
}
.agent-team-window.is-dragging { user-select: none; }

.team-window-outcome {
  margin: 0 0 var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--color-border-light);
  border-radius: var(--r-md);
  background: var(--color-bg-page);
}
.team-window-outcome strong { display: block; margin-bottom: 4px; font-size: var(--fs-sm); }
.team-window-outcome ul { margin: 0; padding-left: 18px; line-height: 1.6; }
.team-window-outcome li { overflow-wrap: anywhere; }
.team-window-outcome.is-success { border-color: var(--sev-low, #8fd19e); }
.team-window-outcome.is-danger { border-color: var(--sev-high, #e58c8c); }
.team-window-outcome.is-warning { border-color: var(--sev-medium, #d9a857); }
.team-window-task-result,
.team-window-task-reason { margin: 4px 0 0; line-height: 1.55; overflow-wrap: anywhere; }
.team-window-task-reason { color: var(--sev-high, #b91c1c); }
.team-window-task-instructions summary { cursor: pointer; color: var(--color-text-secondary); }
.team-window-task-instructions p { margin: 4px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }
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

/* ── 强化进度条 ─────────────────────────────── */
.team-window-progress {
  padding: var(--sp-2) var(--sp-3) calc(var(--sp-2) + 2px);
  border-bottom: 1px solid var(--color-border-light);
}
.team-window-progress-top {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  margin-bottom: 5px;
}
.team-window-progress-pct {
  font-size: 15px;
  font-weight: 700;
  background: linear-gradient(90deg, var(--brand-500), var(--accent-500));
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
.team-window-progress-frac { color: var(--color-text-secondary); font-size: 11px; }
.team-window-progress-mini { margin-left: auto; color: var(--color-text-placeholder); font-size: 10px; white-space: nowrap; }
.team-window-progress-bar {
  position: relative;
  height: 8px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--gray-100);
}
.team-window-progress-bar i {
  position: relative;
  display: block;
  height: 100%;
  overflow: hidden;
  border-radius: inherit;
  background: linear-gradient(90deg, var(--brand-400), var(--accent-400));
  transition: width 0.4s ease;
}
/* 运行中流光:一条斜纹高光在已完成部分往复扫过 */
.team-window-progress-bar.is-active i::after {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(115deg, transparent 20%, rgba(255, 255, 255, 0.55) 50%, transparent 80%);
  transform: translateX(-100%);
  animation: team-progress-shine 1.6s ease-in-out infinite;
}
@keyframes team-progress-shine {
  0% { transform: translateX(-100%); }
  60%, 100% { transform: translateX(100%); }
}

.team-window-body {
  min-height: 0;
  flex: 1;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: var(--sp-2) var(--sp-3);
}
.team-window-section { min-width: 0; padding-top: var(--sp-2); }
.team-window-section:first-child { padding-top: 0; }

/* ── 成员工作卡片区:纵向卡片列表(Codex 风格) ── */
.team-window-members-section { display: grid; gap: 8px; }

/* ── 群聊气泡 ──────────────────────────────── */
.team-window-chat { display: grid; gap: 8px; }
.team-chat-row { display: flex; align-items: flex-start; gap: 7px; min-width: 0; }
.team-chat-avatar {
  display: grid;
  place-items: center;
  flex: none;
  width: 26px;
  height: 26px;
  margin-top: 1px;
  border-radius: 50%;
  color: #fff;
  font-size: 12px;
  font-weight: 650;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.18);
}
/* 角色配色:小菱=品牌紫,执行=青,验证=金,汇总=绿 */
.team-chat-avatar.is-manager { background: linear-gradient(135deg, var(--brand-500), var(--brand-400)); }
.team-chat-avatar.is-worker { background: linear-gradient(135deg, var(--accent-500), var(--accent-400)); }
.team-chat-avatar.is-verifier { background: linear-gradient(135deg, var(--sev-medium), #e8c07a); }
.team-chat-avatar.is-summarizer { background: linear-gradient(135deg, var(--color-success), #7fce9b); }
.team-chat-avatar.is-user { background: linear-gradient(135deg, #64748b, #94a3b8); }
.team-chat-avatar.is-system { background: linear-gradient(135deg, #475569, #64748b); }
.team-chat-main { display: flex; flex-direction: column; align-items: flex-start; min-width: 0; max-width: calc(100% - 33px); }
.team-chat-head { display: flex; align-items: baseline; gap: 5px; min-width: 0; max-width: 100%; margin-bottom: 2px; }
.team-chat-name { overflow: hidden; font-weight: 600; font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.team-chat-badge {
  flex: none;
  padding: 0 5px;
  border-radius: 999px;
  font-size: 9.5px;
  line-height: 14px;
}
.team-chat-badge.is-worker { background: rgba(61, 188, 217, 0.12); color: var(--accent-600); }
.team-chat-badge.is-verifier { background: var(--sev-medium-bg); color: var(--sev-medium); }
.team-chat-badge.is-summarizer { background: var(--color-success-light); color: var(--color-success); }
.team-chat-badge.is-manager { background: var(--brand-50); color: var(--brand-600); }
.team-chat-badge.is-user,
.team-chat-badge.is-system { background: var(--color-bg-page); color: var(--color-text-secondary); }
.team-chat-mention {
  flex: none;
  max-width: 140px;
  overflow: hidden;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--brand-50);
  color: var(--brand-600);
  font-size: 9.5px;
  line-height: 14px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.team-chat-time { flex: none; margin-left: auto; color: var(--color-text-placeholder); font-size: 9.5px; }

.team-chat-bubble {
  min-width: 0;
  max-width: 100%;
  padding: 6px 10px;
  border: 1px solid var(--color-border-light);
  border-radius: 3px 12px 12px;
  background: var(--gray-50);
}
.team-chat-bubble.is-manager { border-color: var(--brand-200); background: var(--brand-50); }
.team-chat-bubble.is-worker { border-color: rgba(61, 188, 217, 0.3); background: rgba(61, 188, 217, 0.08); }
.team-chat-bubble.is-verifier { border-color: rgba(216, 166, 87, 0.35); background: var(--sev-medium-bg); }
.team-chat-bubble.is-summarizer { border-color: rgba(91, 181, 125, 0.35); background: var(--color-success-light); }
.team-chat-subject { margin: 0; overflow-wrap: anywhere; line-height: 1.5; }
.team-chat-payload {
  margin: 3px 0 0;
  padding-top: 3px;
  border-top: 1px dashed var(--color-border-light);
  color: var(--color-text-secondary);
  font-size: 10.5px;
  overflow-wrap: anywhere;
}
.team-chat-payload-text {
  max-width: 100%;
  margin: 0;
  color: inherit;
  font: inherit;
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.team-chat-payload-text.is-expanded {
  max-height: min(36vh, 220px);
  overflow: auto;
  overscroll-behavior: contain;
}
.team-chat-payload-actions { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 4px; }
.team-chat-payload-actions button {
  min-height: 28px;
  padding: 2px 0;
  border: 0;
  background: transparent;
  color: var(--brand-600);
  font: inherit;
  cursor: pointer;
}
.team-chat-payload-actions button:hover { text-decoration: underline; }
.team-chat-payload-actions button:focus-visible { outline: 2px solid var(--brand-500); outline-offset: 2px; }

.team-window-empty { margin: var(--sp-3) 0; color: var(--color-text-placeholder); text-align: center; }

/* ── 任务明细(默认折叠) ────────────────────── */
.team-window-task-details { padding-top: var(--sp-2); }
.team-window-task-details > summary {
  display: flex;
  align-items: center;
  min-height: 40px;
  cursor: pointer;
  color: var(--color-text-secondary);
  font-size: 11px;
  font-weight: 650;
  user-select: none;
}
.team-window-task-details > summary:hover { color: var(--brand-600); }
.team-window-tasks { display: grid; gap: 4px; margin: 5px 0 0; padding: 0; list-style: none; }
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
.team-window-task-error-content {
  display: block;
  max-width: 100%;
  max-height: min(30vh, 180px);
  margin-top: 4px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 10px;
  overscroll-behavior: contain;
}

.team-window-item-status { flex: none; color: var(--color-text-secondary); font-size: 10px; white-space: nowrap; }

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
  .agent-team-window { inset: auto 12px 12px 12px !important; width: auto; max-height: calc(100dvh - 24px); }
  /* 触控适配:关闭按钮加大到 40px */
  .team-window-drag { display: none; }
  .team-window-close { width: 40px; height: 40px; }
  .team-window-action { min-height: 40px; }
  .team-chat-payload-actions button { min-height: 36px; }
  .team-chat-payload-text.is-expanded { max-height: min(32vh, 200px); }
  .team-window-task-error-content { max-height: min(28vh, 160px); }
  .team-window-progress-top { flex-wrap: wrap; }
  .team-window-progress-mini { flex: 1 0 100%; margin-left: 0; white-space: normal; }
}

@media (max-height: 520px) and (min-width: 521px) {
  .agent-team-window {
    inset: auto 12px 12px auto !important;
    width: min(520px, calc(100vw - 24px));
    max-height: calc(100dvh - 24px);
  }
  .team-window-drag { display: none; }
  .team-window-close { width: 40px; height: 40px; }
  .team-window-action { min-height: 40px; }
  .team-window-progress-top { flex-wrap: wrap; }
  .team-window-progress-mini { flex: 1 0 100%; margin-left: 0; white-space: normal; }
}

@media (prefers-reduced-motion: reduce) {
  .team-window-progress-bar i,
  .team-window-action,
  .team-window-enter-active,
  .team-window-leave-active { transition: none; }
  .team-window-progress-bar.is-active i::after { animation: none; }
}
</style>
