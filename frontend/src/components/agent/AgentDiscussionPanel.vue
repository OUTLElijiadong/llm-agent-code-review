<template>
  <Teleport to="body">
    <div class="discuss-overlay">
      <div class="discuss-room">
        <!-- 顶部 -->
        <header class="room-header">
          <div class="header-main">
            <span class="header-icon">🗣️</span>
            <div class="header-titles">
              <div class="header-title">
                <span class="header-heading">Agent 圆桌讨论</span>
                <span v-if="fileName" class="file-chip">{{ fileName }}</span>
              </div>
              <div class="header-sub">
                <el-tag :type="statusTag.type" size="small" effect="dark" round>
                  {{ statusTag.text }}
                </el-tag>
                <span v-if="totalRounds" class="round-info">
                  第 {{ currentRound }} / {{ totalRounds }} 轮
                </span>
                <span v-if="currentSpeaker" class="speaker-info">
                  · {{ currentSpeaker }} 决策中
                </span>
                <span v-else-if="phase === 'concluded'" class="speaker-info done">
                  · 讨论已结束
                </span>
                <span v-if="isFollowupOpen" class="speaker-info done">
                  · 追问剩余 {{ followupCountdown }}
                </span>
              </div>
            </div>
          </div>
          <div class="header-actions">
            <el-button
              v-if="phase === 'concluded' && reportTaskId"
              type="success"
              size="small"
              round
              class="report-btn"
              @click="goReport"
            >📄 查看报告</el-button>
            <el-tooltip :content="isPaused ? '继续讨论' : '暂停讨论'" placement="bottom">
              <el-button
                circle
                :disabled="!canControl"
                :type="isPaused ? 'success' : 'default'"
                :aria-label="isPaused ? '继续讨论' : '暂停讨论'"
                @click="togglePause"
              >
                <span class="btn-emoji">{{ isPaused ? '▶' : '⏸' }}</span>
              </el-button>
            </el-tooltip>
            <el-tooltip content="终止讨论" placement="bottom">
              <el-button
                circle
                type="danger"
                plain
                :disabled="!canControl"
                aria-label="终止讨论"
                @click="onStop"
              >
                <span class="btn-emoji">⏹</span>
              </el-button>
            </el-tooltip>
            <el-tooltip content="关闭" placement="bottom">
              <el-button circle aria-label="关闭讨论" @click="onClose">
                <span class="btn-emoji">✕</span>
              </el-button>
            </el-tooltip>
          </div>
        </header>

        <div class="room-progress" role="status" aria-label="圆桌执行进度">
          <div class="room-progress-meta">
            <span>{{ progressLabel }}</span>
            <span v-if="progressTotal > 0">{{ progressCompleted }} / {{ progressTotal }}</span>
            <span v-else>等待服务端进度</span>
          </div>
          <div
            class="room-progress-track"
            :class="{ indeterminate: progressTotal <= 0 && phase !== 'concluded' }"
            role="progressbar"
            :aria-valuenow="progressTotal > 0 ? progressCompleted : undefined"
            :aria-valuemax="progressTotal > 0 ? progressTotal : undefined"
            aria-valuemin="0"
            :aria-valuetext="`${progressLabel}，${progressTotal > 0 ? `${progressCompleted} / ${progressTotal}` : '尚无计数'}`"
          >
            <span :style="{ width: `${progressPercent}%` }" />
          </div>
        </div>

        <!-- 参会者 -->
        <div class="participants">
          <span class="part-label">参会:</span>
          <span class="part-chip orchestrator">🎤 主持人</span>
          <span
            v-for="a in agents"
            :key="a.code"
            class="part-chip"
            :class="participantClass(a.code)"
            :style="{ '--chip-color': themeOf(a.code).color }"
          >
            {{ themeOf(a.code).icon }} {{ a.name }}
            <span v-if="participantStateLabel(a.code)" class="part-state">
              {{ participantStateLabel(a.code) }}
            </span>
          </span>
          <span class="part-chip user">🙋 你</span>
        </div>

        <!-- 消息区 -->
        <div ref="msgContainer" class="room-body" @scroll="onScroll">
          <div v-if="hasEarlier || historyLoading || historyError" class="history-controls">
            <button v-if="hasEarlier" class="load-earlier" type="button" :disabled="historyLoading" @click="loadEarlier">
              {{ historyLoading ? '正在加载…' : '加载更早发言' }}
            </button>
            <span v-if="historyError" role="status">{{ historyError }}</span>
            <button v-if="historyError" class="load-earlier" type="button" @click="syncLatest()">重试同步</button>
          </div>
          <div v-if="lastError" class="error-bar">
            <span>⚠️ {{ lastError }}</span>
            <el-button size="small" type="primary" plain @click="reconnect">重新连接</el-button>
          </div>

          <div v-if="turns.length === 0 && !lastError" class="empty-state">
            <PrismLoading v-if="phase !== 'concluded'" label="讨论准备中" sublabel="Agent 正在就位,即将开始发言" compact />
            <EmptyState v-else description="本次讨论没有产生发言" :image-size="90" />
          </div>

          <TransitionGroup name="msg" tag="div">
            <div
              v-for="turn in turns"
              :key="turnKey(turn)"
              class="msg-row"
              :class="rowClass(turn)"
            >
              <div class="msg-avatar" :style="avatarStyle(turn)">
                {{ avatarIcon(turn) }}
              </div>
              <div class="msg-main">
                <div class="msg-meta">
                  <span class="msg-name" :style="{ color: nameColor(turn) }">
                    {{ turn.role === 'user' ? '你' : turn.agent_name }}
                  </span>
                  <span
                    v-if="showStance(turn)"
                    class="stance-badge"
                    :class="`stance-${turn.stance || 'neutral'}`"
                  >
                    {{ stanceLabel(turn.stance) }}
                  </span>
                  <span v-if="turn.action === 'silent'" class="silent-badge">静音</span>
                  <span v-if="turn.reply_to" class="reply-target">
                    回应 {{ agentName(turn.reply_to) }}
                  </span>
                  <span class="msg-time">{{ formatTime(turn.timestamp) }}</span>
                </div>
                <div
                  class="msg-bubble"
                  :class="bubbleClass(turn)"
                >
                  <span v-if="turn.action === 'silent'" class="silent-reason">
                    {{ turn.content }}
                  </span>
                  <div v-else v-html="render(turn.content)" />
                </div>
              </div>
            </div>
          </TransitionGroup>

          <!-- 正在发言指示 -->
          <div v-if="currentSpeaker && phase !== 'concluded'" class="msg-row agent typing-row">
            <div class="msg-avatar" :style="{ background: typingColor }">
              {{ typingIcon }}
            </div>
            <div class="msg-main">
              <div class="msg-meta">
                <span class="msg-name" :style="{ color: typingColor }">{{ currentSpeaker }}</span>
              </div>
              <div class="msg-bubble typing">
                <span class="dot" /><span class="dot" /><span class="dot" />
              </div>
            </div>
          </div>
        </div>

        <!-- 回到底部 -->
        <button v-if="showScrollBtn" class="scroll-btn" @click="scrollToBottom(true)">↓ 最新</button>

        <!-- 输入区 -->
        <footer class="room-footer">
          <div class="quick-row">
            <span class="quick-label">快捷:</span>
            <el-button
              v-for="q in quickPrompts"
              :key="q.text"
              size="small"
              round
              :disabled="!canSend"
              @click="sendQuick(q.text)"
            >{{ q.icon }} {{ q.label }}</el-button>
          </div>
          <div class="input-row">
            <textarea
              v-model="userMessage"
              class="room-input"
              :placeholder="inputPlaceholder"
              rows="2"
              :disabled="!canSend"
              @keydown="onKeydown"
            />
            <el-button
              type="primary"
              :disabled="!canSend || sendingMessage || !userMessage.trim()"
              @click="sendMessage"
            >发送</el-button>
          </div>
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, nextTick, onBeforeUnmount, onMounted } from 'vue'
import { useRouter } from 'vue-router'

import { renderMarkdown } from '@/utils/markdown'
import EmptyState from '@/components/common/EmptyState.vue'
import PrismLoading from '@/components/common/PrismLoading.vue'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { ElMessage } from 'element-plus/es/components/message/index'
import {
  subscribeDiscussion,
  type WsMessage,
  type DiscussionTurn,
} from '@/utils/discussionStream'
import { getDiscussionSession, type DiscussionProgress } from '@/api/discussion'

const props = defineProps<{
  sessionId: string
  wsUrl?: string
  agents: Array<{ code: string; name: string }>
  fileName?: string
  initialProgress?: DiscussionProgress | null
  initialStatus?: string
  initialReportTaskId?: number
  initialFollowupUntil?: number
  initialTurns?: DiscussionTurn[]
  initialHasEarlier?: boolean
  initialNextBeforeSeq?: number | null
}>()

const emit = defineEmits<{ close: []; settled: [] }>()

const router = useRouter()
const reportTaskId = ref(props.initialReportTaskId || 0)

type ConnStatus = 'connecting' | 'connected' | 'disconnected' | 'error'
type Phase = 'live' | 'concluded'

const turns = ref<DiscussionTurn[]>([...(props.initialTurns || [])].sort((a, b) =>
  Number(a.seq || 0) - Number(b.seq || 0)))
// 已持久化发言以会话内 seq 去重；旧会话帧才回退到 turn_id + timestamp。
const seenKeys = new Set<string>(turns.value.map(turnKey))
function turnKey(t: DiscussionTurn): string {
  return Number(t.seq) > 0 ? `seq:${t.seq}` : `legacy:${t.turn_id}_${t.timestamp}`
}
const hasEarlier = ref(Boolean(props.initialHasEarlier))
const historyLoading = ref(false)
const historyError = ref('')
let nextBeforeSeq = props.initialNextBeforeSeq || 0
let syncPromise: Promise<void> | null = null
let queuedSyncBaseline = Number.POSITIVE_INFINITY
let pollTimer: ReturnType<typeof setInterval> | null = null
let clockTimer: ReturnType<typeof setInterval> | null = null
let disposed = false
const status = ref<ConnStatus>('connecting')
const phase = ref<Phase>(
  ['concluded', 'completed', 'failed', 'cancelled', 'interrupted'].includes(props.initialStatus || '')
    ? 'concluded' : 'live',
)
const lastError = ref('')
const currentRound = ref(0)
const totalRounds = ref(0)
const currentSpeaker = ref('')
const currentSpeakerCode = ref('')
const serverProgress = ref<DiscussionProgress | null>(props.initialProgress ?? null)
const observedSpeakers = ref(turns.value.filter((turn) => turn.role === 'agent' && turn.agent_code !== 'orchestrator').length)
let lastProgressSeq = Number(props.initialProgress?.seq ?? -1)
const terminalStatus = ref('')
const followupUntil = ref(Number(props.initialFollowupUntil) || 0)
const nowSeconds = ref(Date.now() / 1000)
const userMessage = ref('')
const sendingMessage = ref(false)
let pendingMessage = ''
const isPaused = ref(false)
const showScrollBtn = ref(false)
const msgContainer = ref<HTMLElement>()
let stream: ReturnType<typeof subscribeDiscussion> | null = null
let autoScroll = true

const AGENT_THEME: Record<string, { icon: string; color: string }> = {
  security: { icon: '🛡️', color: '#DC4961' },
  reliability: { icon: '🔒', color: '#5B58E8' },
  performance: { icon: '⚡', color: '#D9A857' },
  maintainability: { icon: '🔧', color: '#4FB87A' },
  general: { icon: '👁️', color: '#3BA0FF' },
  orchestrator: { icon: '🎤', color: '#8B5CF6' },
  user: { icon: '🙋', color: '#0EA5E9' },
}
/**
 * 返回指定 Agent 的头像图标和强调色。
 * @param code - Agent 机器编码。
 * @returns Agent 主题；未知编码使用中性兜底主题。
 */
function themeOf(code: string): { icon: string; color: string } {
  return AGENT_THEME[code] ?? { icon: '🤖', color: '#6B7280' }
}

type Stance = NonNullable<DiscussionTurn['stance']>
type DecisionAction = NonNullable<DiscussionTurn['action']>

const STANCE_LABELS: Record<Stance, string> = {
  propose: '提出',
  agree: '赞同',
  oppose: '否认',
  question: '质疑',
  supplement: '补充',
  neutral: '中立',
}

const participantStates = computed<Record<string, { action: DecisionAction; stance: Stance }>>(() => {
  const states: Record<string, { action: DecisionAction; stance: Stance }> = {}
  for (const turn of turns.value) {
    if (turn.role !== 'agent' || turn.agent_code === 'orchestrator') continue
    states[turn.agent_code] = {
      action: turn.action || 'speak',
      stance: turn.stance || 'neutral',
    }
  }
  return states
})

/**
 * 返回参会 Agent 当前应展示的状态文案。
 * @param code - Agent 机器编码。
 * @returns 决策中、静音、已发言或空字符串。
 */
function participantStateLabel(code: string): string {
  if (currentSpeakerCode.value === code) return '决策中'
  const state = participantStates.value[code]
  if (!state) return ''
  return state.action === 'silent' ? '静音' : '已发言'
}

/**
 * 返回参会 Agent 状态对应的 CSS 类。
 * @param code - Agent 机器编码。
 * @returns 用于参会者标签的状态类对象。
 */
function participantClass(code: string): Record<string, boolean> {
  const state = participantStates.value[code]
  return {
    deciding: currentSpeakerCode.value === code,
    silent: currentSpeakerCode.value !== code && state?.action === 'silent',
    spoke: currentSpeakerCode.value !== code && state?.action === 'speak',
  }
}

/**
 * 将结构化立场转换为中文标签。
 * @param stance - 后端返回的立场枚举。
 * @returns 对应中文标签。
 */
function stanceLabel(stance?: DiscussionTurn['stance']): string {
  return STANCE_LABELS[stance || 'neutral']
}

/**
 * 判断一条消息是否需要展示立场标签。
 * @param turn - 当前讨论消息。
 * @returns 普通 Agent 的有效发言返回 true。
 */
function showStance(turn: DiscussionTurn): boolean {
  return turn.role === 'agent'
    && turn.agent_code !== 'orchestrator'
    && turn.action !== 'silent'
}

/**
 * 把回应目标编码转换为参会者名称。
 * @param code - 被回应者 Agent 编码。
 * @returns 参会者名称；未知编码保留原值。
 */
function agentName(code: string): string {
  return props.agents.find((agent) => agent.code === code)?.name || code
}

const quickPrompts = [
  { icon: '🔒', label: '安全', text: '请重点检查安全性问题,例如注入、越权与敏感信息泄露。' },
  { icon: '⚡', label: '性能', text: '请关注性能瓶颈,例如低效循环、N+1 查询与阻塞调用。' },
  { icon: '🔧', label: '可维护性', text: '请检查代码可维护性,例如函数过长、重复代码与命名问题。' },
  { icon: '✅', label: '请汇总', text: '请各位尽快收敛讨论,并由主持人汇总共识结论。' },
]

const statusTag = computed(() => {
  if (phase.value === 'concluded') {
    if (serverProgress.value?.phase === 'partial') return { type: 'warning' as const, text: '部分完成' }
    const terminal = ['failed', 'cancelled', 'interrupted'].includes(terminalStatus.value)
      ? terminalStatus.value : serverProgress.value?.phase || props.initialStatus
    if (terminal === 'failed') return { type: 'danger' as const, text: '失败' }
    if (terminal === 'interrupted') return { type: 'warning' as const, text: '已中断' }
    if (terminal === 'cancelled') return { type: 'info' as const, text: '已取消' }
    return { type: 'info' as const, text: '已结束' }
  }
  if (status.value === 'connecting') return { type: 'warning' as const, text: '连接中' }
  if (status.value === 'connected') return { type: 'success' as const, text: isPaused.value ? '已暂停' : '进行中' }
  if (status.value === 'error') return { type: 'danger' as const, text: '连接错误' }
  return { type: 'danger' as const, text: '已断开' }
})

const canControl = computed(() => status.value === 'connected' && phase.value === 'live')
const isFollowupOpen = computed(() => {
  const complete = terminalStatus.value === 'success' || terminalStatus.value === 'completed'
    || serverProgress.value?.phase === 'completed'
  return phase.value === 'concluded' && complete && followupUntil.value > nowSeconds.value
})
const canSend = computed(() => status.value === 'connected'
  && (phase.value === 'live' || isFollowupOpen.value))
const followupCountdown = computed(() => {
  const remaining = Math.max(0, Math.ceil(followupUntil.value - nowSeconds.value))
  return `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')}`
})
const inputPlaceholder = computed(() => {
  if (phase.value === 'live'
    && ['summarizing', 'extracting', 'reporting'].includes(serverProgress.value?.phase || '')) {
    return '报告正在整理；消息会保存，完成后由主持人按顺序回复…'
  }
  if (phase.value === 'live') return '插话参与讨论…（Enter 发送 / Shift+Enter 换行）'
  if (isFollowupOpen.value) return '报告已完成，五分钟内可继续追问主持人…'
  return '追问窗口已结束，当前圆桌已禁言'
})

const progressTotal = computed(() => {
  const serverTotal = Number(serverProgress.value?.total_units)
  if (Number.isFinite(serverTotal) && serverTotal > 0) return serverTotal
  return totalRounds.value > 0 ? totalRounds.value * props.agents.length : 0
})
const progressCompleted = computed(() => {
  const serverCompleted = Number(serverProgress.value?.completed_units)
  const raw = serverProgress.value && Number.isFinite(serverCompleted)
    ? serverCompleted : observedSpeakers.value
  return Math.max(0, Math.min(progressTotal.value, raw))
})
const progressPercent = computed(() => {
  if (progressTotal.value <= 0) return 0
  const actual = Math.round(progressCompleted.value / progressTotal.value * 100)
  // 完成所有发言并不等于主持汇总、结构化抽取和报告落库成功。
  const completed = phase.value === 'concluded'
    && (serverProgress.value?.phase === 'completed' || terminalStatus.value === 'success')
  return completed ? actual : Math.min(actual, 95)
})
const progressLabel = computed(() => {
  if (phase.value === 'concluded' && serverProgress.value?.phase === 'partial') {
    return '圆桌部分完成，审查覆盖不完整'
  }
  if (phase.value === 'concluded' && ['failed', 'cancelled', 'interrupted'].includes(terminalStatus.value)) {
    return terminalStatus.value === 'failed' ? '圆桌讨论失败'
      : terminalStatus.value === 'cancelled' ? '圆桌讨论已取消' : '圆桌讨论已中断'
  }
  const stage = serverProgress.value?.phase
  if (stage === 'pending') return '准备圆桌讨论'
  if (stage === 'speaking') return '正在发言'
  if (stage === 'summarizing') return '主持人正在汇总'
  if (stage === 'extracting') return '正在整理结构化问题'
  if (stage === 'reporting') return '正在生成报告'
  if (stage === 'completed') return '圆桌讨论已完成'
  if (stage === 'partial') return '圆桌部分完成，审查覆盖不完整'
  if (stage === 'failed') return '圆桌讨论失败'
  if (stage === 'cancelled') return '圆桌讨论已取消'
  if (stage === 'interrupted') return '圆桌讨论已中断'
  if (terminalStatus.value && terminalStatus.value !== 'success') return '圆桌讨论未完成'
  if (phase.value === 'concluded') return '圆桌讨论已结束'
  if (currentSpeaker.value) return '发言进度'
  return progressTotal.value > 0 ? '发言进度' : '准备圆桌讨论'
})

const typingColor = computed(() => themeOf(currentSpeakerCode.value).color)
const typingIcon = computed(() => themeOf(currentSpeakerCode.value).icon)

// 长讨论中每条消息每次重渲染都会重新 markdown 渲染,导致卡顿。
// 用 Map 缓存渲染结果,key 为原文,命中即返回,避免重复渲染。
const renderCache = new Map<string, string>()
function render(text: string): string {
  const key = text || ''
  const cached = renderCache.get(key)
  if (cached !== undefined) return cached
  const html = renderMarkdown(key)
  // 缓存上限,防止超长讨论内存膨胀
  if (renderCache.size > 500) renderCache.clear()
  renderCache.set(key, html)
  return html
}

function rowClass(t: DiscussionTurn) {
  if (t.role === 'user') return 'user'
  if (t.agent_code === 'orchestrator') return 'orchestrator'
  if (t.action === 'silent') return 'agent decision-silent'
  return 'agent'
}
function bubbleClass(t: DiscussionTurn) {
  if (t.role === 'user') return 'bubble-user'
  if (t.agent_code === 'orchestrator') return 'bubble-orch'
  if (t.action === 'silent') return 'bubble-silent'
  return 'bubble-agent'
}
function avatarIcon(t: DiscussionTurn) {
  if (t.role === 'user') return '🙋'
  return themeOf(t.agent_code).icon
}
function avatarStyle(t: DiscussionTurn) {
  return { background: t.role === 'user' ? AGENT_THEME.user.color : themeOf(t.agent_code).color }
}
function nameColor(t: DiscussionTurn) {
  return t.role === 'user' ? AGENT_THEME.user.color : themeOf(t.agent_code).color
}

function latestSeq(): number {
  return turns.value.reduce((latest, turn) => Math.max(latest, Number(turn.seq) || 0), 0)
}

function earliestSeq(): number {
  return turns.value.reduce((earliest, turn) => {
    const seq = Number(turn.seq) || 0
    return seq > 0 && (earliest === 0 || seq < earliest) ? seq : earliest
  }, 0)
}

/** REST 和 WS 共用有序合并，避免重连重放、分页与实时帧产生重复气泡。 */
function addTurn(turn: DiscussionTurn): boolean {
  if (!turn || typeof turn.content !== 'string') return false
  const key = turnKey(turn)
  if (seenKeys.has(key)) return false
  seenKeys.add(key)
  const seq = Number(turn.seq) || 0
  if (seq > 0) {
    const index = turns.value.findIndex((item) => Number(item.seq) > seq)
    if (index >= 0) turns.value.splice(index, 0, turn)
    else turns.value.push(turn)
  } else {
    turns.value.push(turn)
  }
  if (turn.role === 'agent' && turn.agent_code !== 'orchestrator') observedSpeakers.value++
  return true
}

/** 同步尾页，并在尾页与当前已见序号之间有断档时向前翻页补齐。 */
function syncLatest(baselineSeq = latestSeq()): Promise<void> {
  queuedSyncBaseline = Math.min(queuedSyncBaseline, baselineSeq)
  if (syncPromise) return syncPromise
  syncPromise = (async () => {
    while (!disposed && Number.isFinite(queuedSyncBaseline)) {
      const baseline = queuedSyncBaseline
      queuedSyncBaseline = Number.POSITIVE_INFINITY
      try {
        let page = await getDiscussionSession(props.sessionId, 100)
        if (disposed) return
        for (const turn of page.turns || []) addTurn(turn)
        // WS done/progress/session_end 可能在断线或队列溢出时丢失。
        // 账本对账必须同步会话元数据，不能只补聊天气泡和截止时间。
        if (page.progress && Number(page.progress.seq) >= lastProgressSeq) {
          handleControl({ action: 'progress', payload: page.progress as unknown as Record<string, unknown> })
        }
        if (page.status === 'concluded') {
          const wasLive = phase.value === 'live'
          phase.value = 'concluded'
          const finalStage = String(page.progress?.phase || '')
          terminalStatus.value = finalStage === 'completed' ? 'success' : finalStage
          reportTaskId.value = Number(page.report_task_id) || reportTaskId.value
          followupUntil.value = Number(page.followup_until) || 0
          if (wasLive) emit('settled')
        }
        let first = Number(page.turns?.[0]?.seq) || 0
        while (baseline > 0 && first > baseline + 1 && page.has_earlier) {
          page = await getDiscussionSession(props.sessionId, 100, first)
          if (disposed) return
          const olderFirst = Number(page.turns?.[0]?.seq) || 0
          if (!olderFirst || olderFirst >= first) break
          for (const turn of page.turns || []) addTurn(turn)
          first = olderFirst
        }
        if (earliestSeq() === 1) hasEarlier.value = false
        else if (page.has_earlier || earliestSeq() > 1) hasEarlier.value = true
        historyError.value = ''
      } catch {
        if (!disposed) historyError.value = '发言同步失败，请重试'
        break
      }
    }
  })().finally(() => { syncPromise = null })
  return syncPromise
}

async function loadEarlier(): Promise<void> {
  if (historyLoading.value || !hasEarlier.value) return
  const before = earliestSeq() || nextBeforeSeq
  if (!before) return
  historyLoading.value = true
  historyError.value = ''
  const container = msgContainer.value
  const oldHeight = container?.scrollHeight || 0
  const oldTop = container?.scrollTop || 0
  try {
    const page = await getDiscussionSession(props.sessionId, 100, before)
    if (disposed) return
    for (const turn of page.turns || []) addTurn(turn)
    nextBeforeSeq = Number(page.next_before_seq) || 0
    hasEarlier.value = Boolean(page.has_earlier)
    await nextTick()
    if (container) container.scrollTop = oldTop + container.scrollHeight - oldHeight
  } catch {
    if (!disposed) historyError.value = '加载更早发言失败，请重试'
  } finally {
    historyLoading.value = false
  }
}

function connectWs() {
  lastError.value = ''
  stream?.close()
  stream = subscribeDiscussion(
    props.sessionId,
    (msg: WsMessage) => {
      if (msg.type === 'discuss') {
        if (msg.turn.role === 'user' && pendingMessage && msg.turn.content === pendingMessage) {
          acknowledgeMessage()
        }
        const previousLatest = latestSeq()
        if (!addTurn(msg.turn)) return
        if (previousLatest > 0 && Number(msg.turn.seq) > previousLatest + 1) void syncLatest(previousLatest)
        if (msg.turn.agent_code === currentSpeakerCode.value) {
          currentSpeaker.value = ''
          currentSpeakerCode.value = ''
        }
        scrollToBottom()
      } else if (msg.type === 'control') {
        handleControl(msg)
      } else if (msg.type === 'session_end') {
        phase.value = 'concluded'
        followupUntil.value = Number(msg.followup_until) || followupUntil.value
        currentSpeaker.value = ''
        currentSpeakerCode.value = ''
        emit('settled')
        void syncLatest()
      }
    },
    {
      wsUrl: props.wsUrl,
      onStatus: (s) => {
        status.value = s
        if (s === 'connected') lastError.value = ''
        if (s === 'connected' || s === 'disconnected') void syncLatest()
      },
      onError: (m) => { lastError.value = m },
    },
  )
}

function handleControl(msg: { action: string; payload: Record<string, unknown> }) {
  switch (msg.action) {
    case 'progress': {
      const seq = Number(msg.payload.seq)
      if (Number.isFinite(seq) && seq < lastProgressSeq) break
      if (Number.isFinite(seq)) lastProgressSeq = seq
      const completed = Number(msg.payload.completed_units)
      const total = Number(msg.payload.total_units)
      serverProgress.value = {
        phase: String(msg.payload.phase || 'pending'),
        completed_units: Number.isFinite(completed) ? completed : 0,
        total_units: Number.isFinite(total) ? total : 0,
        current_round: Number(msg.payload.current_round) || 0,
        speaker_code: String(msg.payload.speaker_code || ''),
        seq: Number.isFinite(seq) ? seq : lastProgressSeq,
      }
      if (serverProgress.value.current_round > 0) currentRound.value = serverProgress.value.current_round
      if (Number(msg.payload.turn_count) > latestSeq()) void syncLatest()
      break
    }
    case 'round_start':
      currentRound.value = (msg.payload.round as number) || 0
      totalRounds.value = (msg.payload.total_rounds as number) || 0
      break
    case 'speaker':
      currentSpeaker.value = (msg.payload.speaker_name as string) || ''
      currentSpeakerCode.value = (msg.payload.speaker_code as string) || ''
      scrollToBottom()
      break
    case 'paused':
      isPaused.value = true
      break
    case 'resumed':
      isPaused.value = false
      break
    case 'stopping':
      ElMessage.info('讨论正在终止,主持人将给出小结…')
      break
    case 'done':
      phase.value = 'concluded'
      terminalStatus.value = String(msg.payload?.status || '')
      followupUntil.value = Number(msg.payload?.followup_until) || followupUntil.value
      currentSpeaker.value = ''
      currentSpeakerCode.value = ''
      reportTaskId.value = (msg.payload?.task_id as number) || reportTaskId.value
      emit('settled')
      void syncLatest()
      break
    case 'input_accepted':
      acknowledgeMessage()
      break
    case 'input_rejected':
      lastError.value = String(msg.payload?.reason || msg.payload?.message || '消息未被圆桌接受，请确认讨论状态后重试')
      sendingMessage.value = false
      pendingMessage = ''
      break
  }
}

function reconnect() {
  currentSpeaker.value = ''
  currentSpeakerCode.value = ''
  // 保留已确认发言和进度；服务端重放用 seenKeys 去重，避免重连期间内容闪空。
  connectWs()
}

function sendMessage() {
  const text = userMessage.value.trim()
  if (!text || !canSend.value || sendingMessage.value) return
  pendingMessage = text
  sendingMessage.value = true
  if (!stream?.send('user_input', { content: text })) {
    sendingMessage.value = false
    pendingMessage = ''
    lastError.value = '连接暂时不可用，消息仍在输入框中，请重连后发送'
  }
}

function acknowledgeMessage() {
  if (pendingMessage && userMessage.value.trim() === pendingMessage) userMessage.value = ''
  pendingMessage = ''
  sendingMessage.value = false
}

function sendQuick(text: string) {
  if (!canSend.value) return
  stream?.send('user_input', { content: text })
}

function togglePause() {
  if (!canControl.value) return
  if (isPaused.value) {
    stream?.send('resume')
    isPaused.value = false
  } else {
    stream?.send('pause')
    isPaused.value = true
  }
}

async function onStop() {
  if (!canControl.value) return
  try {
    await ElMessageBox.confirm('确定终止本次讨论吗?主持人会基于已有发言给出小结。', '终止讨论', {
      confirmButtonText: '终止',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch { return }
  stream?.send('stop')
}

async function onClose() {
  if (canControl.value && turns.value.length > 0) {
    try {
      await ElMessageBox.confirm('讨论仍在进行,关闭后将断开实时连接。确定关闭?', '关闭讨论', {
        confirmButtonText: '关闭',
        cancelButtonText: '继续讨论',
        type: 'warning',
      })
    } catch { return }
  }
  stream?.close()
  emit('close')
}

function goReport() {
  if (!reportTaskId.value) return
  stream?.close()
  emit('close')
  router.push({ name: 'ReportDetail', params: { id: reportTaskId.value } })
}

function onKeydown(e: KeyboardEvent) {
  if (e.isComposing || e.keyCode === 229) return
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    sendMessage()
  }
}

function onScroll() {
  const el = msgContainer.value
  if (!el) return
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60
  autoScroll = atBottom
  showScrollBtn.value = !atBottom
}

function scrollToBottom(force = false) {
  if (!force && !autoScroll) return
  nextTick(() => {
    const el = msgContainer.value
    if (el) {
      el.scrollTop = el.scrollHeight
      showScrollBtn.value = false
      autoScroll = true
    }
  })
}

function formatTime(ts: string) {
  if (!ts) return ''
  try {
    return new Date(ts).toLocaleTimeString('zh-CN', {
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    })
  } catch { return '' }
}

onMounted(() => {
  // WS 队列满时后端可能只把新发言写入账本，周期性对账可补齐无后续帧的尾段。
  clockTimer = setInterval(() => { nowSeconds.value = Date.now() / 1000 }, 1000)
  pollTimer = setInterval(() => {
    if (status.value === 'connected') void syncLatest()
  }, 20_000)
})
onBeforeUnmount(() => {
  disposed = true
  if (pollTimer) clearInterval(pollTimer)
  if (clockTimer) clearInterval(clockTimer)
  stream?.close()
})
connectWs()
</script>

<style scoped lang="scss">
.discuss-overlay {
  position: fixed;
  inset: 0;
  z-index: 3200;
  background: rgba(15, 18, 40, 0.45);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.discuss-room {
  width: 780px;
  max-width: 96vw;
  height: 84dvh;
  max-height: 880px;
  background: #fff;
  border-radius: 16px;
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.35);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  position: relative;
}

/* 顶部 */
.room-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  background: linear-gradient(135deg, #1f2452, #2b2f6b);
  color: #fff;
  flex-shrink: 0;
}
.header-main { display: flex; align-items: center; gap: 12px; min-width: 0; }
.header-icon { font-size: 24px; }
.header-titles { min-width: 0; }
.header-title {
  font-size: 16px; font-weight: 700; display: flex; align-items: center; gap: 8px; min-width: 0;
}
.header-heading { white-space: nowrap; flex: 0 0 auto; }
.file-chip {
  font-size: 11px; font-weight: 500; padding: 1px 8px; border-radius: 8px;
  background: rgba(255, 255, 255, 0.16); color: #dfe3ff;
  min-width: 0; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.header-sub {
  display: flex; align-items: center; gap: 8px; margin-top: 4px;
  font-size: 12px; color: #b9bdf0;
}
.round-info { color: #dfe3ff; }
.speaker-info { color: #8fd0ff; }
.speaker-info.done { color: #9aa0d8; }
.header-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.btn-emoji { font-size: 13px; line-height: 1; }

.room-progress { padding: 9px 20px 11px; background: #292e67; color: #e4e7ff; flex-shrink: 0; }
.room-progress-meta { display: flex; justify-content: space-between; gap: 10px; font-size: 11px; line-height: 1.4; }
.room-progress-track { height: 5px; margin-top: 7px; overflow: hidden; border-radius: 999px; background: rgba(255, 255, 255, 0.2); }
.room-progress-track > span { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, #8fc8ff, #b8afff); transition: width 0.25s ease; }
.room-progress-track.indeterminate > span { width: 36% !important; animation: discuss-progress 1.4s ease-in-out infinite alternate; }
@keyframes discuss-progress { from { transform: translateX(-110%); } to { transform: translateX(290%); } }

/* 参会者 */
.participants {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 10px 20px; background: #f6f7fb;
  border-bottom: 1px solid var(--color-border-light, #e8e8ef); flex-shrink: 0;
}
.part-label { font-size: 12px; color: var(--color-text-secondary, #888); }
.part-chip {
  font-size: 12px; padding: 3px 10px; border-radius: 12px;
  background: #fff; border: 1px solid var(--chip-color, #d8d8e2);
  color: #444; display: inline-flex; align-items: center; gap: 4px;
  border-left: 3px solid var(--chip-color, #d8d8e2);
}
.part-chip.deciding { background: #eef7ff; box-shadow: inset 0 0 0 1px var(--chip-color); }
.part-chip.silent { opacity: 0.68; background: #f3f4f6; }
.part-chip.spoke { background: #f5fbf7; }
.part-state {
  margin-left: 2px; padding-left: 6px; border-left: 1px solid #d8d8e2;
  font-size: 10px; color: #6b7280;
}
.part-chip.orchestrator { border-color: #8B5CF6; border-left-color: #8B5CF6; color: #6D28D9; }
.part-chip.user { border-color: #0EA5E9; border-left-color: #0EA5E9; color: #0369A1; }

/* 消息区 */
.room-body {
  flex: 1; overflow-y: auto; padding: 18px 20px;
  display: flex; flex-direction: column; gap: 16px;
  background: #fbfbfd;
  &::-webkit-scrollbar { width: 6px; }
  &::-webkit-scrollbar-thumb { background: #d4d4de; border-radius: 3px; }
}

.error-bar {
  display: flex; align-items: center; justify-content: space-between;
  background: #fff1f0; border: 1px solid #ffccc7; border-radius: 8px;
  padding: 10px 14px; font-size: 13px; color: #cf1322;
}

.history-controls { display: grid; justify-items: center; gap: 6px; color: #9a5415; font-size: 12px; }
.load-earlier {
  min-height: 36px; padding: 7px 14px; border: 1px solid #dce0f5; border-radius: 999px;
  background: #fff; color: #4547ad; font: inherit; cursor: pointer;
}
.load-earlier:disabled { opacity: 0.65; cursor: wait; }
.load-earlier:focus-visible { outline: 2px solid #5b58e8; outline-offset: 2px; }

.empty-state { margin: auto; padding: 40px 0; }

.msg-row { display: flex; gap: 10px; max-width: 100%; }
.msg-row.user { flex-direction: row-reverse; }

.msg-avatar {
  width: 36px; height: 36px; border-radius: 50%; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 18px; color: #fff; box-shadow: 0 2px 6px rgba(0, 0, 0, 0.12);
}

.msg-main { min-width: 0; max-width: calc(100% - 52px); }
.msg-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.msg-row.user .msg-meta { flex-direction: row-reverse; }
.msg-name { font-size: 13px; font-weight: 600; }
.msg-time { font-size: 11px; color: var(--color-text-placeholder, #aaa); }
.stance-badge, .silent-badge, .reply-target {
  font-size: 10px; line-height: 18px; padding: 0 6px; border-radius: 4px;
  white-space: nowrap;
}
.stance-badge { color: #374151; background: #eef2f7; }
.stance-agree { color: #166534; background: #dcfce7; }
.stance-oppose { color: #991b1b; background: #fee2e2; }
.stance-question { color: #92400e; background: #fef3c7; }
.stance-supplement { color: #075985; background: #e0f2fe; }
.stance-propose { color: #3730a3; background: #e0e7ff; }
.silent-badge { color: #4b5563; background: #e5e7eb; }
.reply-target { color: #6b7280; background: #f3f4f6; }

.msg-bubble {
  padding: 10px 14px; border-radius: 12px; font-size: 13.5px; line-height: 1.7;
  word-break: break-word; color: #2b2b3a;
}
.bubble-agent { background: #fff; border: 1px solid #e9e9f2; border-top-left-radius: 4px; }
.bubble-silent {
  padding: 7px 11px; color: #6b7280; background: #f3f4f6;
  border: 1px dashed #d1d5db; border-top-left-radius: 4px;
}
.decision-silent .msg-avatar { filter: grayscale(0.75); opacity: 0.68; }
.silent-reason { font-size: 12px; font-style: italic; }
.bubble-orch {
  background: linear-gradient(135deg, #f3efff, #fbf7ff);
  border: 1px solid #e3d8ff; border-top-left-radius: 4px;
}
.bubble-user {
  background: linear-gradient(135deg, #e6f4ff, #f0f9ff);
  border: 1px solid #bae0ff; border-top-right-radius: 4px;
}

.msg-bubble :deep(p) { margin: 0 0 8px; }
.msg-bubble :deep(p:last-child) { margin-bottom: 0; }
.msg-bubble :deep(ol), .msg-bubble :deep(ul) { margin: 4px 0; padding-left: 20px; }
.msg-bubble :deep(li) { margin: 3px 0; }
.msg-bubble :deep(strong) { color: #1f2452; }
.msg-bubble :deep(code) {
  background: #f0f0f6; color: #c7254e; padding: 1px 6px; border-radius: 4px;
  font-family: 'JetBrains Mono', 'Fira Code', monospace; font-size: 12px;
}
.msg-bubble :deep(pre) {
  background: #1e1e2e; color: #cdd6f4; border-radius: 8px; padding: 12px;
  overflow-x: auto; font-size: 12px; margin: 8px 0;
  code { background: transparent; color: inherit; padding: 0; }
}

.typing-row .msg-bubble.typing {
  display: inline-flex; gap: 5px; align-items: center; padding: 14px 16px;
  .dot {
    width: 7px; height: 7px; border-radius: 50%; background: #b0b0c8;
    animation: bounce 1.2s infinite;
    &:nth-child(2) { animation-delay: 0.2s; }
    &:nth-child(3) { animation-delay: 0.4s; }
  }
}
@keyframes bounce { 0%, 60%, 100% { transform: translateY(0); opacity: 0.4; } 30% { transform: translateY(-5px); opacity: 1; } }

.msg-enter-active { transition: all 0.3s ease; }
.msg-enter-from { opacity: 0; transform: translateY(10px); }

.scroll-btn {
  position: absolute; bottom: 128px; left: 50%; transform: translateX(-50%);
  background: #1f2452; color: #fff; border: none; border-radius: 16px;
  padding: 5px 14px; font-size: 12px; cursor: pointer;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2); z-index: 5;
}

/* 输入区 */
.room-footer { border-top: 1px solid var(--color-border-light, #e8e8ef); padding: 12px 20px; flex-shrink: 0; background: #fff; }
.quick-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
.quick-label { font-size: 12px; color: var(--color-text-secondary, #888); }
.input-row { display: flex; gap: 10px; align-items: flex-end; }
.room-input {
  flex: 1; min-width: 0; border: 1px solid var(--color-border-light, #d8d8e2); border-radius: 10px;
  padding: 10px 12px; font-size: 13.5px; line-height: 1.5; resize: none; outline: none;
  font-family: inherit; transition: border-color 0.2s;
  &:focus { border-color: var(--brand-400, #6366f1); }
  &:disabled { background: #f5f5f7; cursor: not-allowed; }
}

@media (max-width: 520px) {
  .discuss-overlay { padding: 8px; align-items: stretch; }
  .discuss-room { width: 100%; max-width: none; height: calc(100dvh - 16px); max-height: none; border-radius: 12px; }
  .room-header { display: grid; grid-template-columns: minmax(0, 1fr); gap: 10px; padding: 12px; }
  .header-main { min-width: 0; gap: 8px; }
  .header-icon { flex: 0 0 auto; }
  .header-titles { min-width: 0; flex: 1; }
  .header-title { white-space: nowrap; }
  .header-heading { font-size: 15px; }
  .file-chip { max-width: min(42vw, 150px); }
  .header-sub { flex-wrap: wrap; gap: 4px 8px; }
  .round-info, .speaker-info { white-space: nowrap; }
  .header-actions { grid-column: 1; justify-content: flex-end; flex-wrap: wrap; gap: 7px; }
  .header-actions .el-button { min-width: 40px; min-height: 40px; margin-left: 0; }
  .header-actions .report-btn { margin-right: auto; }
  .room-progress { padding: 9px 12px 11px; }
  .participants { padding: 8px 12px; max-height: 160px; overflow-y: auto; }
  .part-chip { white-space: nowrap; }
  .room-body { min-height: 0; padding: 12px; }
  .room-footer { padding: 10px 12px max(10px, env(safe-area-inset-bottom)); }
  .quick-row { max-height: 80px; overflow-y: auto; }
  .scroll-btn { bottom: 165px; }
}

@media (prefers-reduced-motion: reduce) {
  .room-progress-track > span { transition: none; animation: none !important; }
}
</style>
