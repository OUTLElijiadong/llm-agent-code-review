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
                Agent 圆桌讨论
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
                  · {{ terminalLabel }}
                </span>
                <span v-else-if="stageMessage" class="speaker-info">
                  · {{ stageMessage }}
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
              <el-button circle aria-label="关闭讨论" :disabled="phase === 'stopping'" @click="onClose">
                <span class="btn-emoji">✕</span>
              </el-button>
            </el-tooltip>
          </div>
        </header>

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
            <span v-if="a.temporary" class="part-state temporary-state">临时</span>
            <span v-if="participantStateLabel(a.code)" class="part-state">
              {{ participantStateLabel(a.code) }}
            </span>
          </span>
          <span class="part-chip user">🙋 你</span>
        </div>

        <div
          v-if="phase !== 'concluded'"
          class="discussion-progress"
          role="progressbar"
          :aria-valuemin="0"
          :aria-valuemax="expectedTurns"
          :aria-valuenow="completedTurns"
          :aria-label="`已完成 ${completedTurns} / ${expectedTurns} 个专家发言`"
        >
          <div class="progress-label">
            <span>{{ phase === 'pending' ? '等待启动' : '专家发言进度' }}</span>
            <strong>{{ completedTurns }} / {{ expectedTurns }}</strong>
          </div>
          <div class="progress-track"><span :style="{ width: `${progressPercent}%` }" /></div>
        </div>

        <!-- 消息区 -->
        <div ref="msgContainer" class="room-body" @scroll="onScroll">
          <div v-if="lastError" class="error-bar">
            <span>⚠️ {{ lastError }}</span>
            <el-button size="small" type="primary" plain :disabled="phase === 'stopping'" @click="reconnect">重新连接</el-button>
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
        <button v-if="showScrollBtn" class="scroll-btn" :disabled="phase === 'stopping'" @click="scrollToBottom(true)">↓ 最新</button>

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
              :placeholder="canSend ? '插话参与讨论，所有 Agent 都会看到你的发言（Enter 发送 / Shift+Enter 换行）' : phase === 'pending' ? '任务已排队，等待启动' : phase === 'stopping' ? '正在停止，暂不能发送消息' : '讨论已结束，无法发言'"
              rows="2"
              :disabled="!canSend"
              @keydown="onKeydown"
            />
            <el-button
              type="primary"
              :disabled="!canSend || !userMessage.trim()"
              @click="sendMessage"
            >发送</el-button>
          </div>
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, nextTick, onBeforeUnmount } from 'vue'
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

const props = defineProps<{
  sessionId: string
  wsUrl?: string
  agents: Array<{ code: string; name: string; temporary?: boolean }>
  fileName?: string
}>()

const emit = defineEmits<{ close: [] }>()

const router = useRouter()
const reportTaskId = ref(0)

type ConnStatus = 'connecting' | 'connected' | 'disconnected' | 'error'
type Phase = 'pending' | 'live' | 'stopping' | 'concluded'
type TerminalStatus = 'success' | 'partial' | 'failed' | 'cancelled' | ''

const turns = ref<DiscussionTurn[]>([])
// 已渲染发言的稳定 key 集合 —— 用于去重。WS 断线自动重连后,后端 subscribe() 会把
// session.turns(含用户插话 turn_id=-1 那条)整段重放,若不去重就会重复 push 出现
// 重复 Vue key,导致 TransitionGroup 渲染错乱、用户发言后续无法正确显示。
const seenKeys = new Set<string>()
function turnKey(t: DiscussionTurn): string {
  return `${t.turn_id}_${t.timestamp}`
}
const status = ref<ConnStatus>('connecting')
const phase = ref<Phase>('pending')
const terminalStatus = ref<TerminalStatus>('')
const stageMessage = ref('')
const lastError = ref('')
const currentRound = ref(0)
const totalRounds = ref(0)
const completedTurns = ref(0)
const expectedTurns = ref(0)
const currentSpeaker = ref('')
const currentSpeakerCode = ref('')
const userMessage = ref('')
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
  if (phase.value === 'pending') return { type: 'warning' as const, text: '等待启动' }
  if (phase.value === 'stopping') return { type: 'danger' as const, text: '停止中' }
  if (phase.value === 'concluded') {
    const labels: Record<Exclude<TerminalStatus, ''>, { type: 'success' | 'warning' | 'danger' | 'info'; text: string }> = {
      success: { type: 'success', text: '已完成' },
      partial: { type: 'warning', text: '部分完成' },
      failed: { type: 'danger', text: '失败' },
      cancelled: { type: 'info', text: '已取消' },
    }
    return labels[terminalStatus.value || 'success']
  }
  if (status.value === 'connecting') return { type: 'warning' as const, text: '连接中' }
  if (status.value === 'connected') return { type: 'success' as const, text: isPaused.value ? '已暂停' : '进行中' }
  if (status.value === 'error') return { type: 'danger' as const, text: '连接错误' }
  return { type: 'danger' as const, text: '已断开' }
})
const terminalLabel = computed(() => ({
  success: '讨论已完成',
  partial: '讨论部分完成',
  failed: '讨论失败',
  cancelled: '讨论已取消',
  '': '讨论已结束',
}[terminalStatus.value]))

const canControl = computed(() => status.value === 'connected' && phase.value === 'live')
const canSend = computed(() => status.value === 'connected' && phase.value === 'live')
const progressPercent = computed(() => expectedTurns.value > 0
  ? Math.min(100, Math.floor((completedTurns.value / expectedTurns.value) * 100))
  : 0)

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

function connectWs() {
  lastError.value = ''
  stream?.close()
  stream = subscribeDiscussion(
    props.sessionId,
    (msg: WsMessage) => {
      if (msg.type === 'discuss') {
        const key = turnKey(msg.turn)
        if (seenKeys.has(key)) return   // 重连重放/重复帧: 跳过, 不重复追加
        seenKeys.add(key)
        turns.value.push(msg.turn)
        if (msg.turn.agent_code === currentSpeakerCode.value) {
          currentSpeaker.value = ''
          currentSpeakerCode.value = ''
        }
        scrollToBottom()
      } else if (msg.type === 'control') {
        handleControl(msg)
      } else if (msg.type === 'session_end') {
        phase.value = 'concluded'
        currentSpeaker.value = ''
        currentSpeakerCode.value = ''
      }
    },
    {
      wsUrl: props.wsUrl,
      onStatus: (s) => {
        status.value = s
        if (s === 'connected') lastError.value = ''
      },
      onError: (m) => { lastError.value = m },
    },
  )
}

function handleControl(msg: { action: string; payload: Record<string, unknown> }) {
  switch (msg.action) {
    case 'snapshot': {
      const payload = msg.payload
      currentRound.value = Number(payload.round) || 0
      totalRounds.value = Number(payload.total_rounds) || 0
      currentSpeaker.value = String(payload.speaker_name || '')
      currentSpeakerCode.value = String(payload.speaker_code || '')
      stageMessage.value = String(payload.message || '')
      completedTurns.value = Math.max(0, Number(payload.completed_turns) || 0)
      expectedTurns.value = Math.max(0, Number(payload.expected_turns) || 0)
      isPaused.value = payload.status === 'paused'
      reportTaskId.value = Number(payload.report_task_id) || 0
      const savedTerminal = String(payload.terminal_status || '') as TerminalStatus
      if (['success', 'partial', 'failed', 'cancelled'].includes(savedTerminal)) {
        terminalStatus.value = savedTerminal
        phase.value = 'concluded'
        currentSpeaker.value = ''
        currentSpeakerCode.value = ''
      } else {
        terminalStatus.value = ''
        phase.value = payload.status === 'pending' ? 'pending' : 'live'
      }
      break
    }
    case 'started':
      phase.value = 'live'
      stageMessage.value = String(msg.payload.message || '圆桌已启动，主持人正在安排讨论')
      break
    case 'progress':
      completedTurns.value = Math.max(0, Number(msg.payload.completed_turns) || 0)
      expectedTurns.value = Math.max(0, Number(msg.payload.expected_turns) || 0)
      stageMessage.value = String(msg.payload.message || '')
      break
    case 'steering_received':
      stageMessage.value = String(msg.payload.message || '已收到你的补充，小菱会先调整后续讨论')
      break
    case 'round_start':
      currentRound.value = (msg.payload.round as number) || 0
      totalRounds.value = (msg.payload.total_rounds as number) || 0
      break
    case 'speaker':
      currentSpeaker.value = (msg.payload.speaker_name as string) || ''
      currentSpeakerCode.value = (msg.payload.speaker_code as string) || ''
      stageMessage.value = String(currentSpeaker.value) + ' 正在决策'
      scrollToBottom()
      break
    case 'phase':
      stageMessage.value = String(msg.payload.message || '')
      break
    case 'paused':
      isPaused.value = true
      break
    case 'resumed':
      isPaused.value = false
      break
    case 'stopping':
      phase.value = 'stopping'
      terminalStatus.value = 'cancelled'
      stageMessage.value = '讨论正在终止，不会生成成功小结'
      ElMessage.info('正在停止圆桌讨论')
      break
    case 'done':
      phase.value = 'concluded'
      currentSpeaker.value = ''
      currentSpeakerCode.value = ''
      reportTaskId.value = (msg.payload?.task_id as number) || 0
      terminalStatus.value = ['success', 'partial', 'failed', 'cancelled'].includes(String(msg.payload?.status || ''))
        ? String(msg.payload.status) as TerminalStatus
        : 'success'
      stageMessage.value = String(msg.payload?.error || '')
      break
  }
}

function reconnect() {
  if (phase.value === 'stopping') return
  turns.value = []
  seenKeys.clear()
  currentRound.value = 0
  totalRounds.value = 0
  currentSpeaker.value = ''
  currentSpeakerCode.value = ''
  phase.value = 'pending'
  terminalStatus.value = ''
  stageMessage.value = ''
  isPaused.value = false
  connectWs()
}

function sendMessage() {
  const text = userMessage.value.trim()
  if (!text || !canSend.value) return
  stream?.send('user_input', { content: text })
  userMessage.value = ''
}

function sendQuick(text: string) {
  if (!canSend.value) return
  stream?.send('user_input', { content: text })
  ElMessage.success('已发送给讨论组')
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
    await ElMessageBox.confirm('确定终止本次讨论吗？停止后不会生成成功小结，已完成发言会保留。', '终止讨论', {
      confirmButtonText: '终止',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch { return }
  phase.value = 'stopping'
  currentSpeaker.value = ''
  currentSpeakerCode.value = ''
  stageMessage.value = '正在停止，等待当前模型请求退出'
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

onBeforeUnmount(() => stream?.close())
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
  height: 84vh;
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
  font-size: 16px; font-weight: 700; display: flex; align-items: center; gap: 8px;
}
.file-chip {
  font-size: 11px; font-weight: 500; padding: 1px 8px; border-radius: 8px;
  background: rgba(255, 255, 255, 0.16); color: #dfe3ff;
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
.part-state.temporary-state { color: #7c3aed; background: #f5f0ff; border: 0; padding: 1px 5px; border-radius: 8px; }
.part-chip.orchestrator { border-color: #8B5CF6; border-left-color: #8B5CF6; color: #6D28D9; }
.part-chip.user { border-color: #0EA5E9; border-left-color: #0EA5E9; color: #0369A1; }

.discussion-progress { padding: 8px 20px 10px; background: #fff; border-bottom: 1px solid #ececf3; }
.progress-label { display: flex; justify-content: space-between; align-items: center; color: #697086; font-size: 11px; margin-bottom: 5px; }
.progress-label strong { color: #4438a8; font-variant-numeric: tabular-nums; }
.progress-track { height: 5px; overflow: hidden; border-radius: 999px; background: #ececf4; }
.progress-track span { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, #7c65f5, #34b9c8); transition: width 240ms ease; }

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
  flex: 1; border: 1px solid var(--color-border-light, #d8d8e2); border-radius: 10px;
  padding: 10px 12px; font-size: 13.5px; line-height: 1.5; resize: none; outline: none;
  font-family: inherit; transition: border-color 0.2s;
  &:focus { border-color: var(--brand-400, #6366f1); }
  &:disabled { background: #f5f5f7; cursor: not-allowed; }
}
</style>
