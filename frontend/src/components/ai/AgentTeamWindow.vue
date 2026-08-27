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
import AgentMemberWorkCard from '@/components/ai/AgentMemberWorkCard.vue'
import { useFloatingChatPosition } from '@/composables/useFloatingChatPosition'

/**
 * 子 Agent 团队独立悬浮窗(Codex 风格多 Agent 群聊视图)。
 *
 * 区别于主聊天面板(AgentChatDrawer):这是一个可拖拽的独立浮窗,
 * 把一个子 Agent 团队渲染成「群聊」——每条协作消息是一个带头像/角色
 * 配色的气泡,成员卡片实时展示「正在做什么」,顶部是强化进度条,
 * 任务明细默认折叠,并提供「重试失败」「取消团队」操作入口。
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
/** 气泡滚动容器:新消息到达时自动滚到底部。 */
const chatBodyRef = ref<HTMLElement | null>(null)

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
/** 团队仍在推进(运行/验证/排队)时进度条加流光动画。 */
const teamActive = computed(() => {
  const status = props.team?.status
  return Boolean(status && ['running', 'verifying', 'queued'].includes(status))
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
const ROLE_LABELS: Record<string, string> = {
  worker: '执行', verifier: '验证', summarizer: '汇总',
}
/** 小菱/系统侧地址关键字:命中即视为主持人(品牌紫头像)。 */
const MANAGER_TOKENS = ['manager', 'user', 'system', 'coordinator', 'orchestrator', '小菱', 'session:']

function teamStatusLabel(status: string): string {
  return TEAM_STATUS_LABELS[status] ?? status
}
function taskStatusLabel(status: string): string {
  return TASK_STATUS_LABELS[status] ?? status
}
function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role
}
function statusClass(status: string): string {
  return `is-${status.replace(/[^a-z0-9_-]/gi, '-')}`
}

/** 归一化地址:小写并去掉 agent:/member: 等前缀,便于与 member_key/address 比对。 */
function normalizeAddress(raw?: string | null): string {
  return (raw ?? '').trim().toLowerCase().replace(/^(agent|member|session|user|system)[:/]+/, '')
}

function findMember(raw?: string | null): AgentTeamMember | undefined {
  const key = normalizeAddress(raw)
  if (!key) return undefined
  return members.value.find((member) => {
    const candidates = [member.member_key, member.address, member.display_name]
    return candidates.some((candidate) => candidate && normalizeAddress(candidate) === key)
  })
}

/** 发言者身份:优先按角色配色(worker/verifier/summarizer),否则视为小菱/系统主持人。 */
interface Speaker {
  key: string
  name: string
  /** 气泡头部角色徽标文案;小菱/系统为空(不展示徽标)。 */
  badge: string
  /** 头像内单字。 */
  initial: string
  /** 配色类别 → CSS 修饰类。 */
  theme: 'manager' | 'worker' | 'verifier' | 'summarizer'
}

function resolveSpeaker(raw?: string | null): Speaker {
  const key = normalizeAddress(raw) || 'system'
  const member = findMember(raw)
  if (member) {
    const theme = ROLE_LABELS[member.role] ? (member.role as Speaker['theme']) : 'manager'
    return {
      key,
      name: member.display_name,
      badge: roleLabel(member.role),
      initial: (member.display_name || roleLabel(member.role) || '员').trim().charAt(0),
      theme,
    }
  }
  const text = (raw ?? '').trim()
  if (!text || MANAGER_TOKENS.some((token) => key.includes(token))) {
    return { key, name: '小菱', badge: '', initial: '菱', theme: 'manager' }
  }
  // 未在成员列表里的其他地址:按未知成员处理,用执行色兜底
  return { key, name: text, badge: '', initial: text.charAt(0).toUpperCase() || '员', theme: 'worker' }
}

/**
 * 「@某人」小标签:send_to 指向具体成员时展示「指派 @xx」,广播/回到小菱时不展示。
 * task.assign 类消息用「指派」,其余用「回复」。
 */
function mentionLabel(message: AgentTeamMessage): string {
  const target = findMember(message.send_to)
  if (!target) return ''
  if (normalizeAddress(message.send_to) === normalizeAddress(message.sent_from)) return ''
  const verb = /assign|dispatch|delegate/i.test(message.message_type) ? '指派' : '回复'
  return `${verb} @${target.display_name}`
}

/** 每个成员的实时工作状态已由 AgentMemberWorkCard 渲染(状态+计时+日志)。 */

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

/** 新消息/成员状态变化时,气泡区自动滚动到底部。 */
watch(
  () => [messages.value.length, props.team?.updated_at],
  async () => {
    if (!props.visible) return
    await nextTick()
    const el = chatBodyRef.value
    if (el) el.scrollTop = el.scrollHeight
  },
  { flush: 'post' },
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

        <div ref="chatBodyRef" class="team-window-body">
          <!-- 成员实时工作卡片(Codex 风格:头像+状态+计时+操作日志) -->
          <section v-if="members.length" class="team-window-section team-window-members-section" aria-label="团队成员实时状态">
            <AgentMemberWorkCard
              v-for="member in members"
              :key="member.member_id"
              :member="member"
              :tasks="tasks"
              :events="props.team?.events ?? []"
              :team-started-at="team.started_at"
              show-ask
              @ask="team && emit('ask-member', { teamId: team.team_id, name: $event.display_name, address: $event.address })"
            />
          </section>

          <!-- 多 Agent 群聊气泡流 -->
          <section v-if="messages.length" class="team-window-section team-window-chat" aria-label="协作消息">
            <div
              v-for="message in messages"
              :key="message.message_id"
              class="team-chat-row"
            >
              <span
                class="team-chat-avatar"
                :class="`is-${resolveSpeaker(message.sent_from).theme}`"
                aria-hidden="true"
              >{{ resolveSpeaker(message.sent_from).initial }}</span>
              <div class="team-chat-main">
                <div class="team-chat-head">
                  <span class="team-chat-name">{{ resolveSpeaker(message.sent_from).name }}</span>
                  <span
                    v-if="resolveSpeaker(message.sent_from).badge"
                    class="team-chat-badge"
                    :class="`is-${resolveSpeaker(message.sent_from).theme}`"
                  >{{ resolveSpeaker(message.sent_from).badge }}</span>
                  <span v-if="mentionLabel(message)" class="team-chat-mention">{{ mentionLabel(message) }}</span>
                  <time class="team-chat-time">{{ formatTime(message.create_time ?? message.created_at) }}</time>
                </div>
                <div class="team-chat-bubble" :class="`is-${resolveSpeaker(message.sent_from).theme}`">
                  <p class="team-chat-subject">{{ message.subject || message.message_type }}</p>
                  <p v-if="message.payload" class="team-chat-payload">{{ summarize(message.payload) }}</p>
                </div>
              </div>
            </div>
          </section>

          <p v-if="!members.length && !messages.length" class="team-window-empty">
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
                <details v-if="task.errors?.length" class="team-window-task-error">
                  <summary>错误信息</summary>
                  <code>{{ formatFullDetail(task.errors) }}</code>
                </details>
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
  z-index: 3200;
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

.team-window-empty { margin: var(--sp-3) 0; color: var(--color-text-placeholder); text-align: center; }

/* ── 任务明细(默认折叠) ────────────────────── */
.team-window-task-details { padding-top: var(--sp-2); }
.team-window-task-details > summary {
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
.team-window-task-error code { display: block; margin-top: 4px; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 10px; }

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
  .agent-team-window { inset: auto 12px 12px 12px !important; width: auto; }
  /* 触控适配:关闭按钮加大到 40px */
  .team-window-drag { display: none; }
  .team-window-close { width: 40px; height: 40px; }
  .team-window-action { min-height: 40px; }
}

@media (prefers-reduced-motion: reduce) {
  .team-window-progress-bar i,
  .team-window-action,
  .team-window-enter-active,
  .team-window-leave-active { transition: none; }
  .team-window-progress-bar.is-active i::after { animation: none; }
}
</style>
