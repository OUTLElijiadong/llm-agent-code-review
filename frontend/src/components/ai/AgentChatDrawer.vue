<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Check, CircleCheck, CircleCloseFilled, Close, Connection, CopyDocument, Upload, WarningFilled } from '@element-plus/icons-vue'

import { renderMarkdown } from '@/utils/markdown'
import dayjs from 'dayjs'
import { post } from '@/api/http'
import { cancelAgentResponseRun, getAgentResponseSession } from '@/api/agentResponses'
import { archiveAgentMeshSession, type AgentMeshMessage } from '@/api/agentMesh'
import { getAgentTeam, listAgentTeams, type AgentTeamDetail, type AgentTeamSummary } from '@/api/agentTeams'
import { getProjects, createProject, updateProject, deleteProject } from '@/api/project'
import { upload as uploadCodeFile } from '@/api/codeFile'
import { getReviewTasks } from '@/api/review'
import AgentAvatar from '@/components/agent/AgentAvatar.vue'
import AgentNavLink from '@/components/ai/AgentNavLink.vue'
import AgentSessionSwitcher from '@/components/ai/AgentSessionSwitcher.vue'
import PrismMascot from '@/components/ai/PrismMascot.vue'
import AiOrb from '@/components/common/AiOrb.vue'
import FluidProgress from '@/components/common/FluidProgress.vue'
import ResponseApprovalCard from '@/components/ai/responses/ResponseApprovalCard.vue'
import ResponseInputCard from '@/components/ai/responses/ResponseInputCard.vue'
import ResponseToolTimeline from '@/components/ai/responses/ResponseToolTimeline.vue'
import AgentTeamTrace from '@/components/ai/AgentTeamTrace.vue'
import AgentTeamWindow from '@/components/ai/AgentTeamWindow.vue'
import TaskCancelConfirm from '@/components/ai/TaskCancelConfirm.vue'
import { isPageActionTool, toolRunningPhrase } from '@/utils/toolDisplay'
import { useAgentActivityStore } from '@/stores/agentActivity'
import { extractNavigateDirectives } from '@/utils/agentNavigation'
import type { AgentNavigateDirective } from '@/types/agentGuide'
import type { AgentEvent, ClarifyPayload, ClarifyQuestion } from '@/types/agentEvent'
import type { AgentStatus } from '@/types/agent'
import { ElMessage } from 'element-plus/es/components/message/index'
import { useRouter } from 'vue-router'
import {
  CUSTOM_PROJECT_OPTION_VALUE,
  prepareClarifyAnswers,
  resolveProjectClarifyOptions,
  type ClarifyProjectOption,
} from '@/utils/clarifyProjectOptions'
import { streamResponses } from '@/utils/responsesStream'
import {
  agentResponseSessionPollInterval,
  isAgentResponseSessionActive,
  isAgentResponseSessionOccupied,
  isAgentResponseSessionWaiting,
} from '@/utils/agentResponseSession'
import { normalizeAgentText } from '@/utils/agentText'
import { createAgentMeshBridge } from '@/utils/agentMeshBridge'
import {
  agentMeshToolCalls,
  findAgentMeshTimeline,
  settleAgentMeshTimeline,
} from '@/utils/agentMeshTimeline'
import { useFloatingChatPosition } from '@/composables/useFloatingChatPosition'
import { buildAutoValidationPrompt } from '@/utils/autoValidation'
import {
  autoTitleAgentChatSession,
  loadAgentChatSnapshot,
  saveActiveAgentChatSession,
  saveAgentChatSnapshot,
  type AgentChatSessionMeta,
  type AgentChatSnapshotMessage,
  type AgentChatSnapshotTeam,
} from '@/utils/agentChatSessions'
import {
  applyResponseToolEvent,
  attachApprovalToToolCall,
  attachInputToToolCall,
  finishResponseToolCalls,
  isResponseToolEvent,
  responseToolCallsFromEvents,
  setResponseToolCallStatus,
  type ResponseToolCall,
  type ResponseToolCallStatus,
} from '@/utils/responsesTimeline'
import type {
  ResponseApprovalRequiredEvent,
  ResponseApprovalDecision,
  ResponseInputRequiredEvent,
  ResponsesStreamHandle,
  ResponseStreamEvent,
} from '@/types/responses'

interface StepBubble {
  agent: string
  type: AgentEvent['type']
  message: string
  time: string
}

/**
 * v3.0 双层调度单步调用结果(对齐后端 PlanStepOut)
 */
interface PlanStep {
  step_index: number
  tool_name: string
  reason: string
  arguments: Record<string, unknown>
  success: boolean
  duration_ms: number
  error?: string | null
  data_preview?: string | null
}

interface ChatMessage {
  id?: string
  role: 'user' | 'assistant' | 'error'
  content: string
  time: string
  /** 错误卡片:失败后留在消息流里,带「重试」与「新建对话」 */
  errorCard?: { retryable: boolean }
  runId?: string
  trace_id?: string
  steps?: StepBubble[]
  clarify?: ClarifyPayload
  /** v3.0 双层调度调用链(空表示未触发双层调度) */
  planSteps?: PlanStep[]
  approval?: ResponseApprovalRequiredEvent & {
    status: 'pending' | 'submitting' | 'approved' | 'rejected'
  }
  inputRequest?: ResponseInputRequiredEvent & {
    answer: string
    answerSent?: boolean
    status: 'pending' | 'submitting' | 'answered'
  }
  toolCalls?: ResponseToolCall[]
  /** 审计四阶段进度(侦察→分析→验证→汇报),由 response.audit.progress 累积 */
  auditPhases?: Array<{ phase: string; label: string; message: string }>
  /** 助手回复末尾解析出的"带我去"导航指令 */
  navigations?: AgentNavigateDirective[]
  /** 这条时间线消息发起时已创建/发现的子 Agent 团队。 */
  teamIds?: number[]
}

const props = defineProps<{ visible: boolean; prefill?: string }>()
const emit = defineEmits<{ 'update:visible': [value: boolean]; 'consumed-prefill': [] }>()

const router = useRouter()
/** 全局「小菱工作中」活动信号:驱动彩框与虚拟鼠标。 */
const activityStore = useAgentActivityStore()

const MASCOT_NAME = '小菱'
const WELCOME_TEXT = `你好呀,我是${MASCOT_NAME},Prism 棱镜智能代码审查平台的小助手!我可以帮你发起代码审查、解读报告、查询项目与漏洞。点击左上角「+」可以随时开新对话,多个任务我会并行帮你盯着。`

/** 空状态快捷问题:点击填入输入框并直接发送。 */
const QUICK_QUESTIONS = ['帮我发起代码审查', '查看我的项目', '有什么安全问题']

const messages = ref<ChatMessage[]>([])
const inputText = ref('')
const loading = ref(false)
const showTyping = ref(false)
const modelName = ref('deepseek-v4-flash')
const chatBody = ref<HTMLElement>()
const chatInputRef = ref<HTMLTextAreaElement>()
const { panelRef, style: panelStyle, dragging, restoreOrAnchor, beginDrag, moveDrag, endDrag } = useFloatingChatPosition('user')
const LEGACY_SESSION_KEY = 'prism-user-agent-session'
const sessionId = ref('')
let activeResponse: ResponsesStreamHandle | null = null
let sessionRestoreStarted = false
let sessionPollFailures = 0
let sessionPollTimer: number | undefined
let liveTeamPollTimer: number | undefined
let sessionPollStopped = false
let sessionPollGeneration = 0
let sessionSnapshotSignature = ''
const sessionRun = ref<Awaited<ReturnType<typeof getAgentResponseSession>>['run']>(null)
const sessionRestoring = ref(true)
const sessionPollError = ref('')
const sessionLastPolledAt = ref('')
const reconnectHint = ref(false)
let reconnectHintTimer: number | undefined
const cancelPromptVisible = ref(false)
const agentTeams = ref<AgentTeamDetail[]>([])
const cachedAgentTeams = ref<AgentTeamSummary[]>([])
const agentTeamLoading = ref(false)
const agentTeamError = ref('')
const sessionBusy = computed(() => isAgentResponseSessionOccupied(sessionRun.value?.status))

/** 失败/未完成/超轮数的运行可手动重试（回退策略入口） */
const canRetryRun = computed(() => {
  const status = sessionRun.value?.status
  return Boolean(status && ['failed', 'incomplete', 'max_rounds_exceeded'].includes(status) && !loading.value)
})
const switcherRef = ref<InstanceType<typeof AgentSessionSwitcher> | null>(null)
const meshSessions = ref<AgentChatSessionMeta[]>([])
const backgroundBusySessions = new Set<string>()
const lastActiveToolName = ref('')
const uploading = ref(false)
/** 上传进度:completed/total 驱动进度条,phase 描述当前阶段。 */
const uploadProgress = ref({ completed: 0, total: 0, phase: '' })
/** 最近一次失败/取消的运行:支撑错误卡片的「重试最后一条消息」。 */
const lastFailedRun = ref<{ kind: 'user-message' | 'approval' | 'input' }>({ kind: 'user-message' })

/** 子 Agent 团队独立悬浮窗:当前点中查看的团队。 */
const teamWindowVisible = ref(false)
const teamWindowTeamId = ref<number | null>(null)
const teamWindowTeam = computed(() => (
  agentTeams.value.find((team) => team.team_id === teamWindowTeamId.value) ?? null
))
const visibleAgentTeams = computed<Array<AgentTeamDetail | AgentTeamSummary>>(() => {
  const merged = new Map<number, AgentTeamDetail | AgentTeamSummary>()
  for (const team of cachedAgentTeams.value) merged.set(team.team_id, team)
  for (const team of agentTeams.value) merged.set(team.team_id, team)
  return [...merged.values()]
})
const teamById = (teamId: number): AgentTeamDetail | AgentTeamSummary | undefined => (
  visibleAgentTeams.value.find((team) => team.team_id === teamId)
)
const anchoredTeamIds = computed(() => new Set(
  messages.value.flatMap((message) => message.teamIds ?? []),
))
const unanchoredAgentTeams = computed(() => (
  visibleAgentTeams.value.filter((team) => !anchoredTeamIds.value.has(team.team_id))
))

function snapshotTeam(team: AgentTeamDetail | AgentTeamSummary): AgentChatSnapshotTeam {
  return {
    team_id: team.team_id,
    title: team.title,
    objective: team.objective,
    surface: team.surface,
    session_id: team.session_id,
    status: team.status,
    max_active_children: team.max_active_children,
    trace_id: team.trace_id,
    counts: team.counts,
    created_at: team.created_at,
    updated_at: team.updated_at,
  }
}

function restoreCachedTeams(teams: AgentChatSnapshotTeam[] | undefined): void {
  cachedAgentTeams.value = (teams ?? [])
    .filter((team) => team.surface === 'user' && team.session_id === sessionId.value)
    .map((team) => ({ ...team, status: team.status as AgentTeamSummary['status'] }))
}

function openTeamWindow(team: AgentTeamDetail | AgentTeamSummary): void {
  teamWindowTeamId.value = team.team_id
  teamWindowVisible.value = true
}

/** 吉祥物与标题栏共享的agent状态:运行中/等待用户/空闲 */
const mascotStatus = computed<'idle' | 'running' | 'waiting'>(() => {
  const status = sessionRun.value?.status
  if (loading.value || isAgentResponseSessionActive(status)) return 'running'
  if (isAgentResponseSessionWaiting(status)) return 'waiting'
  return 'idle'
})
const runStatusLabel = computed(() => (
  mascotStatus.value === 'running' ? '运行中' : mascotStatus.value === 'waiting' ? '等待你操作' : '空闲'
))

/** 小菱执行进度:统计当前会话中「最新一轮」工具调用的完成情况。 */
const toolStepProgress = computed(() => {
  const activeRunId = sessionRun.value?.run_id
  // 运行中取当前 run 的调用;无 run(或运行已结束)时回退到会话里最后一组调用
  const source = activeRunId
    ? [...messages.value].reverse().find((msg) => msg.runId === activeRunId && msg.toolCalls?.length)
    : [...messages.value].reverse().find((msg) => msg.toolCalls?.length)
  const calls = source?.toolCalls ?? []
  const countable = calls.filter((call) => call.status !== 'rejected')
  const done = countable.filter((call) => ['completed', 'failed'].includes(call.status)).length
  const current = countable.find((call) =>
    ['streaming', 'queued', 'delivered', 'acknowledged', 'processing', 'running'].includes(call.status))
    ?? countable.find((call) => ['waiting_approval', 'waiting_input'].includes(call.status))
  const hasPending = countable.some((call) =>
    !['completed', 'failed'].includes(call.status))
  return { done, total: countable.length, current, hasPending }
})
/** 存在工具调用且未全部完成时才显示执行进度条。 */
const showToolProgress = computed(() => (
  toolStepProgress.value.total > 0 && toolStepProgress.value.hasPending
))
const toolProgressPercent = computed(() => {
  const { done, total } = toolStepProgress.value
  return total > 0 ? Math.round((done / total) * 100) : 0
})
/** 面板是否可发起新的运行(发送/重试共用同一套守卫)。 */
const canStartRun = computed(() => (
  !loading.value
  && !uploading.value
  && !sessionRestoring.value
  && !sessionBusy.value
))
const canSend = computed(() => inputText.value.trim().length > 0 && canStartRun.value)
/** 小菱流式运行中:显示「停止响应」按钮,点击即中止当前流。 */
const canStopResponse = computed(() => (
  loading.value || isAgentResponseSessionActive(sessionRun.value?.status)
))

const meshBridge = createAgentMeshBridge({
  surface: 'user',
  getSessionId: () => sessionId.value,
  getTitle: () => meshSessions.value.find((item) => item.id === sessionId.value)?.title ?? '用户端小菱对话',
  getSessions: () => meshSessions.value,
  getActiveRun: () => sessionRun.value,
  isBusy: isMeshSessionBusy,
  onMessage: handleMeshMessage,
  // 会话被服务端归档(空会话定时清理/他端删除)后,让切换器重新收敛列表并剔除它
  onSessionGone: () => { void switcherRef.value?.refreshFromAgentMesh() },
})

function messageId(): string {
  return crypto.randomUUID()
}

/**
 * 从页面操作类工具参数中提取目标路由/动作语义,供虚拟鼠标定位真实元素。
 * 认识的参数形态:capability 的 page('/admin/users')、project_id 等实体 id
 * 会被 VirtualCursor 的路由匹配进一步消化;这里只做无副作用的字符串提取。
 */
function pageActionTargetHint(args?: string | Record<string, unknown>): string | undefined {
  if (!args) return undefined
  let parsed: Record<string, unknown> | null = null
  if (typeof args === 'string') {
    try {
      parsed = JSON.parse(args) as Record<string, unknown>
    } catch {
      return undefined
    }
  } else if (typeof args === 'object') {
    parsed = args
  }
  if (!parsed) return undefined
  const page = typeof parsed.page === 'string' ? parsed.page : ''
  const params = parsed.params
  const nestedPage = params && typeof params === 'object' && typeof (params as Record<string, unknown>).page === 'string'
    ? (params as Record<string, unknown>).page as string
    : ''
  const route = page || nestedPage
  const action = typeof parsed.action === 'string' ? parsed.action : ''
  if (route || action) {
    return [route, action].filter(Boolean).join(' ').trim()
  }
  return undefined
}

function welcomeMessage(): ChatMessage {
  return {
    id: messageId(),
    role: 'assistant',
    content: WELCOME_TEXT,
    time: dayjs().format('HH:mm'),
  }
}

/** 把当前会话的关键上下文写入本地快照,供切换会话与忙碌标记使用。 */
function persistSnapshot(): void {
  if (!sessionId.value) return
  saveAgentChatSnapshot(sessionId.value, {
    messages: messages.value.map((message) => ({
      role: message.role === 'error' ? 'assistant' : message.role,
      content: message.content,
      teamIds: message.teamIds?.length ? [...message.teamIds] : undefined,
    })),
    teams: visibleAgentTeams.value.map(snapshotTeam),
    runStatus: sessionRun.value?.status ?? null,
    updatedAt: Date.now(),
  })
}

/** 会话切换:中止本地流视图与轮询,清空后展示欢迎语并恢复目标会话。 */
async function handleSessionSelect(nextSessionId: string): Promise<void> {
  // 切换器重挂载等场景会重复发出当前会话的 select:避免中断正在运行的流;
  // 但若没有活动流且未在恢复中,重新拉一次快照兜底。
  if (nextSessionId === sessionId.value) {
    if (!loading.value && !sessionRestoring.value && !activeResponse) {
      sessionRestoreStarted = false
      void restoreSession()
    }
    return
  }
  // 面板重建后内存是初始空状态,不能用它覆盖已有快照;
  // 仅当当前会话有真实内容(非欢迎语)或运行状态时才持久化。
  const hasRealContent = messages.value.some((message) => {
    const content = message.content ?? ''
    return content.trim().length > 0 && content.trim() !== WELCOME_TEXT.trim()
  })
  if (hasRealContent || sessionRun.value?.status) persistSnapshot()
  activeResponse?.abort()
  activeResponse = null
  clearLiveTeamPoll()
  sessionPollStopped = true
  invalidateSessionPoll()
  sessionPollStopped = false
  sessionSnapshotSignature = ''
  sessionRestoreStarted = false
  sessionId.value = nextSessionId
  sessionPollFailures = 0
  cancelPromptVisible.value = false
  sessionRun.value = null
  agentTeams.value = []
  cachedAgentTeams.value = []
  agentTeamError.value = ''
  teamWindowVisible.value = false
  teamWindowTeamId.value = null
  loading.value = false
  showTyping.value = false
  lastActiveToolName.value = ''
  activityStore.clear()
  sessionRestoring.value = true
  messages.value = [welcomeMessage()]
  await nextTick()
  scrollToBottom()
  void restoreSession()
}

function syncBusy(): void {
  // loading 阶段 response.created 尚未到达时也保持忙碌标记,避免打开新对话误切走。
  const busy = loading.value || isAgentResponseSessionOccupied(sessionRun.value?.status)
  if (!busy) backgroundBusySessions.delete(sessionId.value)
  switcherRef.value?.setBusy(sessionId.value, busy)
}

function handleSwitcherReady(metas: AgentChatSessionMeta[]): void {
  meshSessions.value = metas
  syncBusy()
  void meshBridge.syncNow()
}

function isMeshSessionBusy(targetSessionId: string): boolean {
  if (targetSessionId === sessionId.value) {
    return loading.value || sessionRestoring.value || sessionBusy.value
  }
  if (backgroundBusySessions.has(targetSessionId)) return true
  return isAgentResponseSessionOccupied(loadAgentChatSnapshot(targetSessionId)?.runStatus)
}

function restoredMessages(
  session: Awaited<ReturnType<typeof getAgentResponseSession>>,
  restoredTime: string,
): ChatMessage[] {
  return restoredSessionMessages(session, restoredTime, loadAgentChatSnapshot(session.session_id)?.messages)
}

function persistedTeamBuckets(
  persistedMessages: readonly AgentChatSnapshotMessage[] | undefined,
): Map<string, number[][]> {
  const buckets = new Map<string, number[][]>()
  for (const message of persistedMessages ?? []) {
    if (!message.teamIds?.length) continue
    const key = `${message.role}\u0000${message.content}`
    const entries = buckets.get(key) ?? []
    entries.push(message.teamIds)
    buckets.set(key, entries)
  }
  return buckets
}

function takePersistedTeamIds(
  buckets: Map<string, number[][]>,
  role: 'user' | 'assistant',
  content: string,
): number[] | undefined {
  const key = `${role}\u0000${content}`
  const entries = buckets.get(key)
  if (!entries?.length) return undefined
  const ids = entries.shift()
  if (!entries.length) buckets.delete(key)
  return ids?.length ? [...ids] : undefined
}

/** 恢复会话消息:完整历史 + 非终态运行的部分输出(模型已生成但尚未结束的文本)。 */
function restoredSessionMessages(
  session: Awaited<ReturnType<typeof getAgentResponseSession>>,
  restoredTime: string,
  persistedMessages?: readonly AgentChatSnapshotMessage[],
): ChatMessage[] {
  // 早期版本曾把本地欢迎语带入模型上下文并被服务端持久化,恢复时去重
  const teamBuckets = persistedTeamBuckets(persistedMessages)
  const restored: ChatMessage[] = session.messages
    .filter((message) => message.content.trim() !== WELCOME_TEXT.trim())
    .map((message) => {
      if (message.role !== 'assistant') {
        return {
          id: messageId(), role: message.role, content: message.content, time: restoredTime,
          teamIds: takePersistedTeamIds(teamBuckets, message.role, message.content),
        }
      }
      const { cleaned, directives } = extractNavigateDirectives(message.content)
      return {
        id: messageId(),
        role: message.role,
        content: compactOutsideCodeBlocks(cleaned),
        time: restoredTime,
        navigations: directives.length ? directives : undefined,
        teamIds: takePersistedTeamIds(teamBuckets, message.role, message.content),
      }
    })
  const toolCalls = [
    ...agentMeshToolCalls(session.mesh_messages, `session:user:${session.session_id}`),
    ...responseToolCallsFromEvents(session.events),
  ]
  const emptyTeamAnchors = (persistedMessages ?? [])
    .filter((message) => message.role === 'assistant' && !message.content.trim() && message.teamIds?.length)
    .flatMap((message) => message.teamIds ?? [])
  if (!toolCalls.length && !emptyTeamAnchors.length) return restored
  const timeline: ChatMessage = {
    id: messageId(),
    role: 'assistant',
    content: '',
    time: restoredTime,
    runId: session.run?.run_id,
    toolCalls,
  }
  // 本地快照中的空时间线消息就是团队卡片的稳定锚点。
  if (emptyTeamAnchors.length) timeline.teamIds = [...new Set(emptyTeamAnchors)]
  let conclusionIndex = -1
  for (let index = restored.length - 1; index >= 0; index -= 1) {
    if (restored[index].role === 'assistant' && restored[index].content.trim()) {
      conclusionIndex = index
      break
    }
  }
  restored.splice(conclusionIndex >= 0 ? conclusionIndex : restored.length, 0, timeline)
  // 非终态运行:把模型已生成的部分输出(尚未作为完整消息落库)展示出来。
  const partialOutput = session.run?.output_text?.trim()
  if (
    partialOutput
    && (isAgentResponseSessionOccupied(session.run?.status) || session.run?.status === 'cancelled')
  ) {
    const lastAssistant = [...restored].reverse().find(
      (message) => message.role === 'assistant' && message.content.trim(),
    )
    if (!lastAssistant || lastAssistant.content.trim() !== partialOutput) {
      restored.push({
        id: messageId(),
        role: 'assistant',
        content: partialOutput,
        time: restoredTime,
      })
    }
  }
  // 失败/取消终态:把失败原因留在消息流(错误卡带重试),否则用户只见历史
  // 消息而不知道上次没做完,误以为「已经完成」。
  const failedStatus = session.run?.status
  const failedError = (session.run?.error ?? '').trim()
  if (
    (failedStatus === 'failed' || failedStatus === 'incomplete' || failedStatus === 'max_rounds_exceeded')
    && failedError
  ) {
    restored.push({
      id: messageId(),
      role: 'error',
      content: failedError,
      time: restoredTime,
      errorCard: { retryable: true },
    })
  }
  return restored
}

async function restoreSession(): Promise<void> {
  if (sessionRestoreStarted) return
  sessionRestoreStarted = true
  try {
    const requestedSessionId = sessionId.value
    restoreCachedTeams(loadAgentChatSnapshot(requestedSessionId)?.teams)
    const session = await getAgentResponseSession('user', requestedSessionId)
    // 恢复过程中用户已切到其他会话或发起新流:旧恢复结果作废,不覆盖当前状态。
    if (requestedSessionId !== sessionId.value || loading.value || activeResponse) {
      scheduleSessionPoll()
      return
    }
    sessionRun.value = session.run
    if (session.run?.model) modelName.value = session.run.model
    const restoredTime = session.run?.updated_at
      ? dayjs(session.run.updated_at).format('HH:mm')
      : dayjs().format('HH:mm')
    const restored = restoredMessages(session, restoredTime)
    // 服务端恢复出历史时,按欢迎语+历史整体重建,避免与本地占位重复
    if (restored.length) messages.value = [welcomeMessage(), ...restored]
    sessionSnapshotSignature = JSON.stringify({
      run: session.run,
      messages: session.messages,
      events: session.events,
      mesh_messages: session.mesh_messages,
      pending: session.pending,
    })
    const pending = session.pending
    if (!pending) {
      showTyping.value = isAgentResponseSessionActive(session.run?.status)
      await refreshAgentTeam()
      scheduleSessionPoll()
      return
    }
    const toolCalls: ResponseToolCall[] = []
    const target: ChatMessage = {
      id: messageId(),
      role: 'assistant',
      content: '',
      time: restoredTime,
      runId: session.run?.run_id,
      toolCalls,
    }
    if (pending.type === 'response.approval.required') {
      attachApprovalToToolCall(toolCalls, pending.call_id, pending.tool_name, pending.arguments)
      target.approval = { ...pending, status: 'pending' }
    } else {
      attachInputToToolCall(toolCalls, pending)
      target.inputRequest = { ...pending, answer: '', status: 'pending' }
    }
    messages.value.push(target)
    await refreshAgentTeam()
    scheduleSessionPoll()
  } catch {
    // HTTP 层已给出错误提示；保留本地会话不覆盖用户输入。
  } finally {
    sessionRestoring.value = false
    syncBusy()
    persistSnapshot()
    await nextTick()
    scrollToBottom()
    void meshBridge.syncNow()
  }
}

async function handleMeshMessage(message: AgentMeshMessage, targetSessionId: string): Promise<boolean> {
  if (targetSessionId !== sessionId.value) {
    return runBackgroundMeshMessage(message, targetSessionId)
  }
  if (!findAgentMeshTimeline(messages.value, message.message_id)) {
    messages.value.push({
      id: messageId(),
      role: 'assistant',
      content: '',
      time: dayjs().format('HH:mm'),
      trace_id: message.trace_id,
      toolCalls: agentMeshToolCalls([message], `session:user:${sessionId.value}`),
    })
  }
  await nextTick()
  scrollToBottom()
  const succeeded = await runResponse({
    action: 'start',
    surface: 'user',
    session_id: sessionId.value,
    messages: [],
    mesh_message_id: message.message_id,
  })
  settleAgentMeshTimeline(
    messages.value,
    message.message_id,
    succeeded,
    sessionRun.value?.status,
    sessionRun.value?.error,
  )
  return succeeded
}

async function runBackgroundMeshMessage(
  message: AgentMeshMessage,
  targetSessionId: string,
): Promise<boolean> {
  backgroundBusySessions.add(targetSessionId)
  switcherRef.value?.setBusy(targetSessionId, true)
  let waiting = false
  // 只有 completed 才算成功:failed/incomplete/error 时后端可能把消息重新入队,
  // 这里若返回 true 会被 mesh 桥标记 handled,同 message_id 的重投将被永久跳过。
  let succeeded = false
  const handle = streamResponses({
    action: 'start',
    surface: 'user',
    session_id: targetSessionId,
    messages: [],
    mesh_message_id: message.message_id,
  }, {
    onEvent(event) {
      if (event.type === 'response.approval.required' || event.type === 'response.input.required') {
        waiting = true
      }
      if (event.type === 'response.completed') {
        succeeded = true
      }
      if (
        event.type === 'response.completed'
        || event.type === 'response.incomplete'
        || event.type === 'response.failed'
        || event.type === 'response.cancelled'
        || event.type === 'error'
      ) {
        waiting = false
      }
    },
  })
  try {
    await handle.done
    // 后台会话处理完成:把小菱聚焦到刚活跃起来的这条对话(忙碌/有内容的当前会话不切走)
    if (succeeded) switcherRef.value?.focusSession(targetSessionId)
    return succeeded
  } catch {
    return false
  } finally {
    if (!waiting) {
      backgroundBusySessions.delete(targetSessionId)
      switcherRef.value?.setBusy(targetSessionId, false)
    }
  }
}

function clearSessionPoll(): void {
  if (sessionPollTimer !== undefined) {
    window.clearTimeout(sessionPollTimer)
    sessionPollTimer = undefined
  }
}

function clearLiveTeamPoll(): void {
  if (liveTeamPollTimer !== undefined) {
    window.clearTimeout(liveTeamPollTimer)
    liveTeamPollTimer = undefined
  }
}

/** SSE 运行期间会话轮询暂停，团队账本需要独立轮询才能在创建时立即展示。 */
function scheduleLiveTeamPoll(delay = 0): void {
  clearLiveTeamPoll()
  if (!loading.value || !sessionId.value) return
  const requestedSessionId = sessionId.value
  liveTeamPollTimer = window.setTimeout(async () => {
    liveTeamPollTimer = undefined
    if (!loading.value || requestedSessionId !== sessionId.value) return
    await refreshAgentTeam()
    if (loading.value && requestedSessionId === sessionId.value) scheduleLiveTeamPoll(1000)
  }, delay)
}

function invalidateSessionPoll(): void {
  sessionPollGeneration += 1
  clearSessionPoll()
}

/** 团队状态由服务端账本提供,会话切换和轮询均按当前 session 对齐。 */
async function refreshAgentTeam(generation?: number): Promise<void> {
  if (!sessionId.value) return
  const requestedSessionId = sessionId.value
  const isCurrent = (): boolean => requestedSessionId === sessionId.value
    && (generation === undefined || generation === sessionPollGeneration)
  agentTeamLoading.value = true
  try {
    const listed = await listAgentTeams({ surface: 'user', session_id: requestedSessionId, limit: 20 })
    if (!isCurrent()) return
    if (!listed.items.length) {
      // 短暂网络/账本延迟时不要把已展示的卡片清空;下次轮询会用服务端事实刷新。
      if (!agentTeams.value.length) agentTeams.value = []
      agentTeamError.value = ''
      return
    }
    const details = await Promise.all(listed.items.map((item) => getAgentTeam(item.team_id)))
    if (!isCurrent()) return
    agentTeams.value = details
    cachedAgentTeams.value = details
    const unanchored = details.filter((team) => !anchoredTeamIds.value.has(team.team_id))
    if (unanchored.length) {
      let anchor = [...messages.value].reverse().find((message) => (
        message.role === 'assistant'
        && message.runId === sessionRun.value?.run_id
        && (!message.content.trim() || message.toolCalls?.length)
      ))
      if (!anchor) {
        let conclusionIndex = -1
        for (let index = messages.value.length - 1; index >= 0; index -= 1) {
          if (messages.value[index].role === 'assistant' && messages.value[index].content.trim().length > 0) {
            conclusionIndex = index
            break
          }
        }
        anchor = {
          id: messageId(),
          role: 'assistant',
          content: '',
          time: dayjs().format('HH:mm'),
          runId: sessionRun.value?.run_id,
        }
        messages.value.splice(conclusionIndex >= 0 ? conclusionIndex : messages.value.length, 0, anchor)
      }
      anchor.teamIds = [...new Set([...(anchor.teamIds ?? []), ...unanchored.map((team) => team.team_id)])]
    }
    agentTeamError.value = ''
  } catch {
    if (isCurrent()) agentTeamError.value = '团队状态同步暂时中断'
  } finally {
    if (isCurrent()) agentTeamLoading.value = false
  }
}

function scheduleSessionPoll(immediate = false): void {
  clearSessionPoll()
  if (sessionPollStopped || !sessionId.value) return
  const generation = sessionPollGeneration
  // 网络抖动时指数退避,恢复后立即重试;活跃任务仍保持秒级反馈。
  const base = agentResponseSessionPollInterval(sessionRun.value?.status)
  const backoff = Math.min(sessionPollFailures, 3)
  const delay = immediate ? 0 : base * (2 ** backoff)
  sessionPollTimer = window.setTimeout(() => {
    sessionPollTimer = undefined
    void pollSessionSnapshot(generation)
  }, delay)
}

async function pollSessionSnapshot(generation: number): Promise<void> {
  if (sessionPollStopped || generation !== sessionPollGeneration) return
  try {
    const session = await getAgentResponseSession('user', sessionId.value)
    if (sessionPollStopped || generation !== sessionPollGeneration) return
    sessionPollError.value = ''
    sessionPollFailures = 0
    sessionLastPolledAt.value = dayjs().format('HH:mm:ss')
    await refreshAgentTeam(generation)
    // 活动流持有最新状态,轮询快照可能落后于 SSE,不能在 loading 期间覆盖。
    if (!loading.value) {
      sessionRun.value = session.run
      if (session.run?.model) modelName.value = session.run.model
      const signature = JSON.stringify({
        run: session.run,
        messages: session.messages,
        events: session.events,
        mesh_messages: session.mesh_messages,
        pending: session.pending,
      })
      if (signature !== sessionSnapshotSignature) {
        sessionSnapshotSignature = signature
        const restoredTime = session.run?.updated_at ? dayjs(session.run.updated_at).format('HH:mm') : dayjs().format('HH:mm')
        const restored = restoredMessages(session, restoredTime)
        // 轮询恢复快照:欢迎语置顶 + 服务端历史,替换本地占位
        messages.value = restored.length ? [welcomeMessage(), ...restored] : restored
        const pending = session.pending
        if (pending) {
          const toolCalls: ResponseToolCall[] = []
          const target: ChatMessage = {
            id: messageId(),
            role: 'assistant',
            content: '',
            time: restoredTime,
            runId: session.run?.run_id,
            toolCalls,
          }
          if (pending.type === 'response.approval.required') {
            attachApprovalToToolCall(toolCalls, pending.call_id, pending.tool_name, pending.arguments)
            target.approval = { ...pending, status: 'pending' }
          } else {
            attachInputToToolCall(toolCalls, pending)
            target.inputRequest = { ...pending, answer: '', status: 'pending' }
          }
          messages.value.push(target)
        }
      }
    }
  } catch {
    sessionPollFailures += 1
    sessionPollError.value = '同步暂时中断,正在重试'
  } finally {
    syncBusy()
    persistSnapshot()
    if (!sessionPollStopped && generation === sessionPollGeneration) scheduleSessionPoll()
  }
}

/** 删除代码围栏之外的空白行和旧版展示哨兵。 */
function compactOutsideCodeBlocks(value: string): string {
  return normalizeAgentText(value)
}

/** 流式渲染与历史恢复使用同一文本契约。 */
function formatStreamContent(value: string): string {
  return normalizeAgentText(value)
}

function conversationHistory(): Array<{ role: 'user' | 'assistant'; content: string }> {
  return messages.value
    // 欢迎语是本地开屏气泡,错误卡片是本地展示层,两者都不参与模型上下文
    .filter((message) => (
      message.role !== 'error'
      && message.content.trim().length > 0
      && message.content.trim() !== WELCOME_TEXT.trim()
    ))
    .map((message) => ({ role: message.role as 'user' | 'assistant', content: message.content }))
}

function eventErrorMessage(event: ResponseStreamEvent): string {
  if (event.type === 'error') return event.error?.message || event.message || ''
  if (event.type === 'response.incomplete') return '小菱的回答没说完,重发一次试试'
  if (event.type === 'response.cancelled') return 'Agent 任务已取消'
  if (event.type !== 'response.failed') return ''
  const error = event.response.error
  if (typeof error === 'string') return error
  if (error && typeof error === 'object') {
    const message = (error as Record<string, unknown>).message
    if (typeof message === 'string') return message
  }
  return ''
}

function requestErrorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : '小菱这次没连上,请再试一次'
}

/** 错误留在消息流:失败/取消不再只是 Toast,追加一张带重试/新建对话的错误卡片。 */
function appendErrorCard(content: string, retryable: boolean): void {
  messages.value.push({
    id: messageId(),
    role: 'error',
    content,
    time: dayjs().format('HH:mm'),
    errorCard: { retryable },
  })
  void nextTick().then(scrollToBottom)
}

/** 重试:按最近一次失败的来源续跑(用户消息 / 审批 / 追问),复用发送守卫。 */
async function retryLastAction(): Promise<void> {
  if (!canStartRun.value) return
  const kind = lastFailedRun.value.kind
  if (kind === 'user-message') {
    const lastUserMessage = [...messages.value].reverse().find((message) => message.role === 'user')
    if (!lastUserMessage) return
    await runResponse({
      action: 'start',
      surface: 'user',
      session_id: sessionId.value,
      messages: conversationHistory(),
    })
    return
  }
  if (kind === 'approval') {
    const target = [...messages.value].reverse().find((message) => message.approval?.status === 'pending')
    if (target?.approval) await decideApproval(target, { action: 'approve', confirmation: target.approval.danger ? '确认执行' : '' })
    return
  }
  const target = [...messages.value].reverse().find((message) => message.inputRequest?.status === 'pending')
  if (target) await submitInput(target)
}

/** 新建对话:交给会话切换器创建并切换(正在运行的对话会在后台继续)。 */
function startNewChat(): void {
  switcherRef.value?.createSession()
}

/** 会话归档:服务端成功后才本地移除;失败保留会话并重新发现。 */
/** 团队成员追问:关闭悬浮窗并把追问指令预填到输入框。 */
async function handleAskMember({ name }: { name: string; address: string }): Promise<void> {
  teamWindowVisible.value = false
  inputText.value = `请让「${name}」继续处理，我的要求是：`
  await nextTick()
  // 追问模板全选:打字即替换模板前缀,避免「模板+输入」拼接
  const askInput = chatInputRef.value
  if (askInput) {
    askInput.focus()
    askInput.setSelectionRange(0, askInput.value.length)
  }
  const enabled = !loading.value && !uploading.value && !sessionRestoring.value && !sessionBusy.value
  if (enabled) {
    chatInputRef.value?.focus()
    return
  }
  const stop = watch(
    [loading, uploading, sessionRestoring, sessionBusy],
    () => {
      const canFocus = !loading.value && !uploading.value && !sessionRestoring.value && !sessionBusy.value
      if (!canFocus) return
      stop()
      nextTick(() => chatInputRef.value?.focus())
    },
  )
}

async function handleSessionArchive(sessionId: string): Promise<void> {
  try {
    await archiveAgentMeshSession('user', sessionId)
    switcherRef.value?.removeSession(sessionId)
  } catch {
    ElMessage.warning('服务端归档失败,会话保留在列表中')
    switcherRef.value?.restoreSessionAfterArchiveFailure()
    switcherRef.value?.refreshFromAgentMesh()
  }
}

/**
 * 停止响应:先弹原因确认;确认后调用服务端取消,并把回滚提示留在消息流里。
 */
function requestStopResponse(): void {
  if (!canStopResponse.value) return
  cancelPromptVisible.value = true
}

async function cancelResponse(reason = ''): Promise<void> {
  const runId = sessionRun.value?.run_id
  if (runId && sessionId.value) {
    try {
      await cancelAgentResponseRun('user', sessionId.value, runId, reason)
    } catch {
      // 取消请求失败时仍断开本地流,后续轮询以服务端状态为准。
    }
    if (sessionRun.value) sessionRun.value = { ...sessionRun.value, status: 'cancelled' }
  }
  syncBusy()
  activeResponse?.abort()
  const hint = reason.trim()
    ? `已停止任务（原因：${reason.trim()}），未执行剩余操作；如需继续可重新发起。`
    : '已停止任务，未执行剩余操作；如需继续可重新发起。'
  messages.value.push({ id: messageId(), role: 'assistant', content: hint, time: dayjs().format('HH:mm') })
  await nextTick()
  scrollToBottom()
  scheduleSessionPoll()
}

function handleCancelConfirm(reason: string): void {
  cancelPromptVisible.value = false
  void cancelResponse(reason)
}

/** 消息复制反馈:记录最近复制成功的消息 id,图标短暂变勾。 */
const copiedMessageId = ref('')
let copiedResetTimer: number | undefined

function fallbackCopyText(text: string): boolean {
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.select()
  try {
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    textarea.remove()
  }
}

async function copyMessage(message: ChatMessage): Promise<void> {
  const text = message.content
  let succeeded: boolean
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      succeeded = true
    } else {
      succeeded = fallbackCopyText(text)
    }
  } catch {
    succeeded = fallbackCopyText(text)
  }
  if (!succeeded) {
    ElMessage.error('复制失败,请手动选择文本复制')
    return
  }
  if (!message.id) return
  copiedMessageId.value = message.id
  window.clearTimeout(copiedResetTimer)
  copiedResetTimer = window.setTimeout(() => { copiedMessageId.value = '' }, 1500)
}

function setTimelineCallStatus(
  callId: string | undefined,
  status: ResponseToolCallStatus,
  error?: string,
): void {
  for (let index = messages.value.length - 1; index >= 0; index -= 1) {
    const calls = messages.value[index].toolCalls
    if (calls && setResponseToolCallStatus(calls, callId, status, error)) return
  }
}

function applyExistingTimelineToolEvent(event: ResponseStreamEvent): boolean {
  const callId = 'call_id' in event && typeof event.call_id === 'string' ? event.call_id : undefined
  if (!callId) return false
  for (let index = messages.value.length - 1; index >= 0; index -= 1) {
    const message = messages.value[index]
    const calls = message.toolCalls
    if (!calls?.some((call) => call.callId === callId)) continue
    applyResponseToolEvent(calls, event)
    return true
  }
  return false
}

function finishExistingTimelineToolCalls(runId: string | undefined, error: string): void {
  if (!runId) return
  for (const message of messages.value) {
    if (message.runId !== runId) continue
    finishResponseToolCalls(message.toolCalls ?? [], 'failed', error)
  }
}

async function retryRun(): Promise<void> {
  const runId = sessionRun.value?.run_id
  if (!runId || loading.value || !canRetryRun.value) return
  await runResponse({
    action: 'retry',
    surface: 'user',
    session_id: sessionId.value,
    messages: conversationHistory(),
    run_id: runId,
  })
}

async function runResponse(payload: Record<string, unknown>): Promise<boolean> {
  invalidateSessionPoll()
  clearLiveTeamPoll()
  loading.value = true
  showTyping.value = true
  let rawText = ''
  let textTarget: ChatMessage | null = null
  let timelineTarget: ChatMessage | null = null
  const runToolCalls: ResponseToolCall[] = []
  let activeRunId = sessionRun.value?.run_id
  let protocolError = ''

  const syncTimeline = (): ChatMessage | null => {
    if (!runToolCalls.length) return null
    if (!timelineTarget) {
      timelineTarget = {
        id: messageId(),
        role: 'assistant',
        content: '',
        time: dayjs().format('HH:mm'),
        runId: activeRunId,
        toolCalls: [...runToolCalls],
      }
      messages.value.push(timelineTarget)
    } else {
      timelineTarget.toolCalls = [...runToolCalls]
    }
    return timelineTarget
  }

  /** 应用文本增量:剥离导航指令并渲染为可点击的「前往页面」确认按钮(不自动跳转) */
  const applyTextDelta = (): void => {
    const { cleaned, directives } = extractNavigateDirectives(rawText)
    const content = formatStreamContent(cleaned)
    if (!content.trim()) return
    showTyping.value = false
    if (!textTarget) {
      messages.value.push({
        id: messageId(),
        role: 'assistant',
        content,
        time: dayjs().format('HH:mm'),
      })
      textTarget = messages.value[messages.value.length - 1]
    } else {
      textTarget.content = content
    }
    textTarget.navigations = directives.length ? directives : undefined
  }

  const handle = streamResponses(payload, {
    onEvent(event) {
      if (event.type === 'response.created') {
        const model = event.response.model
        if (typeof model === 'string' && model) modelName.value = model
        const runId = typeof event.response.id === 'string' ? event.response.id : sessionRun.value?.run_id ?? ''
        activeRunId = runId || activeRunId
        sessionRun.value = {
          run_id: runId,
          status: 'running', model: modelName.value, rounds: sessionRun.value?.rounds ?? 0, error: '', updated_at: new Date().toISOString(),
        }
      } else if (event.type === 'response.audit.progress') {
        // 审计四阶段进度:优先挂在当前工具时间线消息上;还没有时间线时自建审计行
        showTyping.value = false
        if (event.phase && event.label) {
          const target = timelineTarget ?? messages.value[messages.value.length - 1]
          if (target && target.auditPhases) {
            const existing = target.auditPhases.find((item) => item.phase === event.phase)
            if (existing) {
              existing.label = event.label
              if (event.message) existing.message = event.message
            } else {
              target.auditPhases.push({ phase: event.phase, label: event.label, message: event.message || '' })
            }
          } else if (target) {
            target.auditPhases = [{ phase: event.phase, label: event.label, message: event.message || '' }]
          } else {
            messages.value.push({
              id: messageId(),
              role: 'assistant',
              content: '',
              time: dayjs().format('HH:mm'),
              auditPhases: [{ phase: event.phase, label: event.label, message: event.message || '' }],
            })
          }
        }
      } else if (isResponseToolEvent(event)) {
        showTyping.value = false
        if (event.type === 'response.tool.started' && typeof event.tool_name === 'string' && event.tool_name) {
          lastActiveToolName.value = event.tool_name
          // 页面操作类工具:点亮全屏彩框 + 虚拟鼠标;targetHint 携带目标路由,
          // 虚拟光标会定位到页面上真实元素并在需要时真实点击跳转
          if (isPageActionTool(event.tool_name)) {
            activityStore.begin(toolRunningPhrase(event.tool_name), event.call_id, pageActionTargetHint(event.arguments))
          }
        } else if (
          (event.type === 'response.tool.completed'
            || event.type === 'response.tool.failed'
            || event.type === 'response.tool.rejected')
          && typeof event.call_id === 'string'
        ) {
          activityStore.end(event.call_id)
        }
        if (!applyExistingTimelineToolEvent(event)) {
          applyResponseToolEvent(runToolCalls, event)
          syncTimeline()
        }
        if (
          typeof event.tool_name === 'string'
          && ['create_agent_team', 'get_agent_team', 'retry_agent_team'].includes(event.tool_name)
          && (event.type === 'response.tool.started' || event.type === 'response.tool.completed')
        ) {
          // create_agent_team.started 时可能尚未落库，独立轮询会持续到 SSE 结束；
          // completed 再立即刷新一次，保证卡片在最终文本输出前出现。
          scheduleLiveTeamPoll(0)
        }
        syncBusy()
      } else if (event.type === 'response.output_text.delta') {
        rawText += event.delta
        applyTextDelta()
      } else if (event.type === 'response.approval.required') {
        showTyping.value = false
        activeRunId = event.run_id
        sessionRun.value = { ...(sessionRun.value ?? { run_id: event.run_id, status: 'running', model: modelName.value, rounds: 0, error: '', updated_at: '' }), run_id: event.run_id, status: 'waiting_approval' }
        clearSessionPoll()
        attachApprovalToToolCall(runToolCalls, event.call_id, event.tool_name, event.arguments)
        const target = syncTimeline()
        const duplicate = messages.value.some((message) => (
          message.approval?.run_id === event.run_id && message.approval.call_id === event.call_id
        ))
        if (!duplicate && target) {
          target.approval = { ...event, status: 'pending' }
        }
      } else if (event.type === 'response.input.required') {
        showTyping.value = false
        activeRunId = event.run_id
        sessionRun.value = { ...(sessionRun.value ?? { run_id: event.run_id, status: 'running', model: modelName.value, rounds: 0, error: '', updated_at: '' }), run_id: event.run_id, status: 'waiting_input' }
        clearSessionPoll()
        attachInputToToolCall(runToolCalls, event)
        const target = syncTimeline()
        const duplicate = messages.value.some((message) => (
          message.inputRequest?.run_id === event.run_id
          && message.inputRequest.call_id === event.call_id
        ))
        if (!duplicate && target) {
          target.inputRequest = { ...event, answer: '', status: 'pending' }
        }
      } else if (
        event.type === 'response.completed'
        || event.type === 'response.incomplete'
        || event.type === 'response.failed'
        || event.type === 'response.cancelled'
        || event.type === 'error'
      ) {
        clearLiveTeamPoll()
        showTyping.value = false
        lastActiveToolName.value = ''
        // 流进入终态:兜底熄灭彩框/虚拟鼠标(单工具结束事件已按 call_id 精确清除)
        activityStore.clear()
        if (sessionRun.value) {
          sessionRun.value = {
            ...sessionRun.value,
            status: event.type === 'response.completed'
              ? 'completed'
              : event.type === 'response.cancelled' ? 'cancelled' : 'failed',
          }
        }
        invalidateSessionPoll()
        syncBusy()
        persistSnapshot()
        protocolError ||= eventErrorMessage(event)
        const terminalError = protocolError || '响应已结束，但工具未返回完成事件'
        finishResponseToolCalls(
          runToolCalls,
          'failed',
          terminalError,
        )
        finishExistingTimelineToolCalls(activeRunId, terminalError)
        syncTimeline()
        // 导航不再自动跳转:PRISM_NAVIGATE 已由 AgentNavLink 渲染为「前往页面」按钮,
        // 是否跳转交给用户点击确认,且跳转不关闭悬浮窗。
      }
      void nextTick().then(scrollToBottom)
    },
  })
  activeResponse = handle

  try {
    await handle.done
    if (protocolError) {
      ElMessage.error(protocolError)
      appendErrorCard(protocolError, true)
      return false
    }
    return true
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      // 用户主动「停止响应」:部分内容保留,留一张可重试的取消卡片。
      appendErrorCard('已停止本次回答,你可以点「重试」继续,或新建对话', true)
      return false
    }
    const text = requestErrorMessage(error)
    ElMessage.error(text)
    appendErrorCard(text, true)
    if (isAgentResponseSessionActive(sessionRun.value?.status)) scheduleSessionPoll()
    return false
  } finally {
    clearLiveTeamPoll()
    if (activeResponse === handle) activeResponse = null
    loading.value = false
    showTyping.value = false
    await nextTick()
    scrollToBottom()
    scheduleSessionPoll()
  }
}

// === Clarify 选项加载 ===
const projectOptions = ref<ClarifyProjectOption[]>([])
const projectSearchOptions = ref<ClarifyProjectOption[]>([])
const projectSearchKeyword = ref('')
const taskOptions = ref<{ value: number; label: string }[]>([])
const projectOptionsLoading = ref(false)

let optionsPromise: Promise<void> | null = null
let projectSearchTimer: number | undefined
let projectSearchRequestId = 0

/**
 * 从项目 API 加载当前用户可见的项目选项。
 * @param keyword - 可选项目名称关键词，后端使用 contains 做模糊查询。
 * @returns 请求完成后更新项目候选，无直接返回值。
 */
async function loadProjectOptions(keyword = ''): Promise<void> {
  const normalizedKeyword = keyword.trim()
  const requestId = ++projectSearchRequestId
  projectOptionsLoading.value = true
  try {
    const data = await getProjects({
      page: 1,
      page_size: 100,
      keyword: normalizedKeyword,
    })
    if (requestId !== projectSearchRequestId) return
    const options = (data.items ?? []).map((p) => ({
      value: p.id,
      label: `#${p.id} ${p.project_name}`,
    }))
    if (normalizedKeyword) projectSearchOptions.value = options
    else projectOptions.value = options
  } catch {
    if (requestId === projectSearchRequestId) {
      if (normalizedKeyword) projectSearchOptions.value = []
      else projectOptions.value = []
    }
  } finally {
    if (requestId === projectSearchRequestId) projectOptionsLoading.value = false
  }
}

/**
 * 确保项目选择器至少加载一次项目 API 首屏。
 * @returns 首次加载中的共享 Promise。
 */
async function ensureProjectOptions(): Promise<void> {
  if (projectOptions.value.length) return
  if (optionsPromise) return optionsPromise
  optionsPromise = (async () => {
    await loadProjectOptions()
    optionsPromise = null
  })()
  return optionsPromise
}

/**
 * 对项目选择器输入做防抖远程查询，避免项目较多时只搜索本地候选子集。
 * @param query - 用户在选择器内输入的项目名称关键词。
 * @returns 无返回值，防抖结束后更新项目候选。
 */
function searchProjectOptions(query: string): void {
  projectSearchKeyword.value = query.trim()
  if (!projectSearchKeyword.value) projectSearchOptions.value = []
  window.clearTimeout(projectSearchTimer)
  projectSearchTimer = window.setTimeout(() => {
    void loadProjectOptions(query)
  }, 250)
}

async function ensureTaskOptions(): Promise<void> {
  if (taskOptions.value.length) return
  try {
    const data = await getReviewTasks({ page: 1, page_size: 100 })
    taskOptions.value = (data.items ?? []).map((t) => ({
      value: t.id,
      label: `#${t.id} ${t.task_name ?? '审查任务'}`,
    }))
  } catch {
    taskOptions.value = []
  }
}

const clarifyAnswers = ref<Record<string, Record<string, string | number>>>({})
const clarifyCustomProjectInputs = ref<Record<string, Record<string, string>>>({})

/**
 * 初始化 Clarify 答案、自定义项目输入和异步候选项。
 * @param clarifyId - Clarify 会话 ID。
 * @param questions - 当前追问问题列表。
 * @returns 无返回值。
 */
function ensureClarifyAnswers(clarifyId: string, questions: ClarifyQuestion[]): void {
  if (!clarifyAnswers.value[clarifyId]) {
    const init: Record<string, string | number> = {}
    // v3.1: 后端模糊命中项目时会下发 default,预填后用户一键即可确认
    for (const q of questions) init[q.key] = q.default ?? ''
    clarifyAnswers.value[clarifyId] = init
  }
  if (!clarifyCustomProjectInputs.value[clarifyId]) {
    const customInputs: Record<string, string> = {}
    for (const q of questions) {
      if (q.type === 'select_project') customInputs[q.key] = ''
    }
    clarifyCustomProjectInputs.value[clarifyId] = customInputs
    projectSearchKeyword.value = ''
    projectSearchOptions.value = []
  }
  // 推荐候选只用于排序，完整项目列表始终从项目 API 加载并合并。
  for (const q of questions) {
    if (q.type === 'select_project') void ensureProjectOptions()
    if (q.type === 'select_task' && !(q.options && q.options.length)) void ensureTaskOptions()
  }
}

/**
 * 合并单个项目追问的推荐项、远程查询结果和自定义入口。
 * @param question - 项目类型 Clarify 问题。
 * @returns 可渲染的完整项目选项列表。
 */
function clarifyProjectOptions(question: ClarifyQuestion): ClarifyProjectOption[] {
  return resolveProjectClarifyOptions(
    question.options ?? [],
    projectOptions.value,
    projectSearchOptions.value,
    projectSearchKeyword.value,
  )
}

/**
 * 判断当前项目问题是否已选择“其他（自定义输入）”。
 * @param clarifyId - Clarify 会话 ID。
 * @param questionKey - 项目问题字段名。
 * @returns 需要展示自定义输入框时返回 true。
 */
function isCustomProjectSelected(clarifyId: string, questionKey: string): boolean {
  return clarifyAnswers.value[clarifyId]?.[questionKey] === CUSTOM_PROJECT_OPTION_VALUE
}

function confirmationQuestion(message: ChatMessage): ClarifyQuestion | undefined {
  return message.clarify?.questions.find((question) => (
    question.type === 'confirm' || question.type === 'danger_confirm'
  ))
}

function submitConfirmation(message: ChatMessage, answer: '确认' | '取消'): void {
  const clarify = message.clarify
  const question = confirmationQuestion(message)
  if (!clarify || !question) return
  clarifyAnswers.value[clarify.clarify_id][question.key] = (
    question.type === 'danger_confirm' && answer === '确认' ? '确认执行' : answer
  )
  void submitClarify(message)
}

/**
 * 提交 Clarify 回答并继续原 Agent 意图。
 * @param message - 携带 Clarify 协议的助手消息。
 * @returns 请求完成后更新对话消息，无直接返回值。
 */
async function submitClarify(message: ChatMessage): Promise<void> {
  const clarify = message.clarify
  if (!clarify) return
  const prepared = prepareClarifyAnswers(
    clarify.questions,
    clarifyAnswers.value[clarify.clarify_id] ?? {},
    clarifyCustomProjectInputs.value[clarify.clarify_id] ?? {},
  )
  if (prepared.missing) {
    ElMessage.warning(`请先回答: ${prepared.missing.label}`)
    return
  }
  loading.value = true
  try {
    const res = await post<{ content: string; clarify?: ClarifyPayload; model?: string }>(
      '/agents/clarify',
      { clarify_id: clarify.clarify_id, answers: prepared.answers },
    )
    message.clarify = undefined
    delete clarifyCustomProjectInputs.value[clarify.clarify_id]
    messages.value.push({
      role: 'assistant',
      content: res.content,
      time: dayjs().format('HH:mm'),
      clarify: res.clarify ?? undefined,
    })
    // v3.1: 多轮追问 — 为新一轮 clarify 初始化答案与候选项
    if (res.clarify) {
      ensureClarifyAnswers(res.clarify.clarify_id, res.clarify.questions)
    }
  } catch {
    ElMessage.error('提交追问失败,请重试')
  } finally {
    loading.value = false
    await nextTick()
    scrollToBottom()
  }
}

const STATUS_BY_TYPE: Record<AgentEvent['type'], AgentStatus> = {
  dispatch: 'thinking',
  thinking: 'thinking',
  progress: 'working',
  complete: 'idle',
  failed: 'error',
  clarify: 'blocked',
  // 安全告警为系统级事件,不改变气泡进度状态,仅展示标签
  admin_alert: 'idle',
}

const TYPE_LABELS: Record<AgentEvent['type'], string> = {
  dispatch: '派发',
  thinking: '思考',
  progress: '进行中',
  complete: '完成',
  failed: '失败',
  clarify: '等待用户',
  admin_alert: '安全告警',
}

function stepStatus(s: StepBubble): AgentStatus {
  return STATUS_BY_TYPE[s.type] ?? 'idle'
}

function stepLabel(s: StepBubble): string {
  return TYPE_LABELS[s.type] ?? s.type
}

async function sendMessage(): Promise<void> {
  const text = inputText.value.trim()
  if (!text || loading.value || sessionRestoring.value || sessionBusy.value) return

  messages.value.push({ id: messageId(), role: 'user', content: text, time: dayjs().format('HH:mm') })
  inputText.value = ''
  lastFailedRun.value = { kind: 'user-message' }
  // 新对话自动命名:首条用户消息提炼为会话标题
  if (autoTitleAgentChatSession('user', sessionId.value, text)) {
    switcherRef.value?.reload?.()
  }

  await nextTick()
  scrollToBottom()
  await runResponse({
    action: 'start',
    surface: 'user',
    session_id: sessionId.value,
    messages: conversationHistory(),
  })
}

/** 空状态快捷问题:填入输入框并直接走现有发送逻辑。 */
function askQuickQuestion(question: string): void {
  if (loading.value || sessionRestoring.value || sessionBusy.value) return
  inputText.value = question
  void sendMessage()
}

async function decideApproval(
  message: ChatMessage,
  decision: ResponseApprovalDecision,
): Promise<void> {
  const approval = message.approval
  if (!approval || approval.status !== 'pending' || loading.value) return
  const { action, confirmation = '' } = decision
  approval.status = 'submitting'
  lastFailedRun.value = { kind: 'approval' }
  setTimelineCallStatus(approval.call_id, action === 'approve' ? 'running' : 'rejected')
  const succeeded = await runResponse({
    action,
    surface: 'user',
    session_id: sessionId.value,
    messages: conversationHistory(),
    run_id: approval.run_id,
    call_id: approval.call_id,
    confirmation,
  })
  approval.status = succeeded ? (action === 'approve' ? 'approved' : 'rejected') : 'pending'
  if (!succeeded) setTimelineCallStatus(approval.call_id, 'failed', '审批续跑失败，可重试')
  else if (action === 'reject') setTimelineCallStatus(approval.call_id, 'rejected')
}

async function submitInput(message: ChatMessage, selectedAnswer?: string): Promise<void> {
  const request = message.inputRequest
  if (request && selectedAnswer !== undefined) request.answer = selectedAnswer
  const answer = request?.answer.trim() ?? ''
  if (!request || request.status !== 'pending' || !answer || loading.value) return
  request.status = 'submitting'
  lastFailedRun.value = { kind: 'input' }
  if (!request.answerSent) {
    messages.value.push({
      id: messageId(),
      role: 'user',
      content: answer,
      time: dayjs().format('HH:mm'),
    })
    request.answerSent = true
  }
  setTimelineCallStatus(request.call_id, 'running')
  const succeeded = await runResponse({
    action: 'answer',
    surface: 'user',
    session_id: sessionId.value,
    messages: conversationHistory(),
    run_id: request.run_id,
    call_id: request.call_id ?? '',
    answer,
  })
  request.status = succeeded ? 'answered' : 'pending'
  setTimelineCallStatus(
    request.call_id,
    succeeded ? 'completed' : 'failed',
    succeeded ? undefined : '提交答案后续跑失败，可重试',
  )
}

function handleKeydown(e: KeyboardEvent): void {
  if (e.isComposing || e.keyCode === 229) return
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    sendMessage()
    return
  }
  // ↑:输入框为空时把最后一条自己发的消息填回来改,避免重复打字
  if (e.key === 'ArrowUp' && !inputText.value.trim()) {
    const lastUserMessage = [...messages.value].reverse().find((message) => message.role === 'user')
    if (!lastUserMessage) return
    e.preventDefault()
    inputText.value = lastUserMessage.content
    void nextTick(() => {
      const input = e.target as HTMLTextAreaElement | null
      if (input) {
        input.focus()
        input.setSelectionRange(input.value.length, input.value.length)
      }
    })
  }
}

/** 面板级快捷键:`/` 从任意处聚焦输入框(输入中不拦截,不影响中文输入)。 */
function handleGlobalKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape' && props.visible) {
    close()
    return
  }
  if (!props.visible || e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return
  const target = e.target as HTMLElement | null
  if (target?.closest('input, textarea, [contenteditable="true"]')) return
  e.preventDefault()
  chatInputRef.value?.focus()
}

/**
 * 计算双层调度调用链的总耗时
 * @param steps - 调用链步骤列表
 * @returns 总耗时(毫秒)
 */
function planTotalMs(steps: PlanStep[]): number {
  return steps.reduce((sum, s) => sum + (s.duration_ms || 0), 0)
}

/**
 * 执行指令导航:仅跳站内路由,目标由 AgentNavLink 同源守卫再次校验。
 * 导航前收起抽屉,让目标页面完整呈现,模拟用户真实浏览路径。
 */
function followNavigation(directive: AgentNavigateDirective): void {
  if (!directive.route.startsWith('/')) return
  // 仅站内跳转,不再关闭悬浮窗——用户可能还要参考对话内容继续操作。
  void router.push(directive.route)
}

/**
 * 拦截助手回复中的站内 markdown 链接点击:
 * 命中路由表则由 AgentNavLink 同源守卫决定是否渲染,点击后 SPA 内跳转,
 * 模拟用户点击真实页面入口;外部链接不拦截。
 */
function onMessageClick(event: MouseEvent): void {
  const anchor = (event.target as HTMLElement | null)?.closest?.('a')
  if (!anchor) return
  const href = anchor.getAttribute('href') ?? ''
  if (!href.startsWith('/') || href.startsWith('//')) return
  event.preventDefault()
  const label = anchor.textContent?.trim() || '前往页面'
  followNavigation({ action: 'navigate', route: href, label })
}

function scrollToBottom(): void {
  if (chatBody.value) {
    chatBody.value.scrollTop = chatBody.value.scrollHeight
  }
}

/* ── 拖拽上传文件建项目 ─────────────────────────────── */
const dragActive = ref(false)
const uploadInput = ref<HTMLInputElement>()

/** 更新上传进度展示:阶段文案 + 已完成/总数驱动进度条。 */
function setUploadProgress(phase: string, completed: number, total: number): void {
  uploadProgress.value = { phase, completed, total }
}

function resetUploadProgress(): void {
  uploadProgress.value = { completed: 0, total: 0, phase: '' }
}

const uploadStatusVisible = computed(() => uploading.value && Boolean(uploadProgress.value.phase))
const uploadPercent = computed(() => {
  const { completed, total } = uploadProgress.value
  return total > 0 ? Math.round((completed / total) * 100) : 0
})

function onDragEnter(event: DragEvent): void {
  if (event.dataTransfer?.types?.includes('Files')) dragActive.value = true
}
function onDragOver(event: DragEvent): void {
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'
}
function onDragLeave(event: DragEvent): void {
  if (!(event.currentTarget as HTMLElement).contains(event.relatedTarget as Node)) {
    dragActive.value = false
  }
}

const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg'])
const CODE_EXTS = new Set(['py', 'js', 'jsx', 'ts', 'tsx', 'vue', 'html', 'css', 'java', 'go', 'php', 'rb', 'c', 'h', 'cpp', 'cc', 'cs', 'rs'])
const LANGUAGE_BY_EXT: Record<string, string> = {
  py: 'Python', js: 'JavaScript', jsx: 'JavaScript', ts: 'TypeScript', tsx: 'TypeScript',
  vue: 'JavaScript', html: 'JavaScript', css: 'JavaScript', java: 'Java', go: 'Go',
  php: 'PHP', rb: 'Ruby', c: 'C', h: 'C', cpp: 'C++', cc: 'C++', cs: 'C#', rs: 'Rust',
}

function inferProjectLanguage(files: File[]): string {
  for (const file of files) {
    const language = LANGUAGE_BY_EXT[file.name.split('.').pop()?.toLowerCase() ?? '']
    if (language) return language
  }
  return 'Python'
}

async function processIncomingFiles(files: File[]): Promise<void> {
  if (!files.length || uploading.value) return
  uploading.value = true
  setUploadProgress(`准备上传…`, 0, files.length)
  try {
    const images = files.filter((f) => IMAGE_EXTS.has(f.name.split('.').pop()?.toLowerCase() ?? ''))
    const codeFiles = files.filter((f) => CODE_EXTS.has(f.name.split('.').pop()?.toLowerCase() ?? ''))
    if (images.length && !codeFiles.length) {
      resetUploadProgress()
      ElMessage.info('图片会作为项目附件上传；若要让小菱帮你创建代码项目，请再拖入至少一个代码文件')
      return
    }
    const targets = files.slice(0, 20)
    const skipped = files.length - targets.length
    if (skipped > 0) ElMessage.warning(`单次最多上传 20 个文件,已跳过后面的 ${skipped} 个`)
    await uploadFilesAsProject(targets, images.length)
  } catch (err) {
    resetUploadProgress()
    ElMessage.error(`上传失败: ${err instanceof Error ? err.message : '请重试'}`)
  } finally {
    uploading.value = false
  }
}

async function onDrop(event: DragEvent): Promise<void> {
  dragActive.value = false
  await processIncomingFiles(Array.from(event.dataTransfer?.files ?? []))
}

async function onFileInput(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  await processIncomingFiles(Array.from(input.files ?? []))
  input.value = ''
}

/** 把拖拽的文件建成一个新项目并导入,然后让 Agent 接手引导下一步。 */
async function uploadFilesAsProject(files: File[], imageCount = 0): Promise<void> {
  setUploadProgress('正在验证文件…', 0, files.length)
  // 创建项目前先逐个读取,空文件/读取失败时不调用创建接口,避免留下空项目。
  const readableFiles: File[] = []
  const preflightFailures: string[] = []
  for (const file of files) {
    try {
      if (file.size <= 0) throw new Error('文件为空')
      await file.slice(0, 1).arrayBuffer()
      readableFiles.push(file)
    } catch (err) {
      preflightFailures.push(`${file.name}: ${err instanceof Error ? err.message : '文件不可读取'}`)
    }
    setUploadProgress('正在验证文件…', readableFiles.length + preflightFailures.length, files.length)
  }
  if (!readableFiles.length) {
    resetUploadProgress()
    throw new Error(`没有可上传的文件${preflightFailures.length ? ` (${preflightFailures[0]})` : ''}`)
  }
  const base = readableFiles.find((file) => !IMAGE_EXTS.has(file.name.split('.').pop()?.toLowerCase() ?? ''))?.name.replace(/\.[^.]+$/, '') || '拖拽上传'
  const suffix = `${new Date().toISOString().slice(5, 10).replace('-', '')}-${crypto.randomUUID().slice(0, 6)}`
  const projectName = `${base}-${suffix}`
  const language = inferProjectLanguage(readableFiles)
  setUploadProgress(`正在创建项目「${projectName}」…`, 0, readableFiles.length)
  const created = await createProject({ project_name: projectName, description: `小菱拖拽上传导入(${readableFiles.map((f) => f.name).join(', ')})`, language })
  const projectId = created.id
  let okCount = 0
  const failures: string[] = [...preflightFailures]
  const targets = readableFiles
  for (let i = 0; i < targets.length; i++) {
    const file = targets[i]
    setUploadProgress(file.name, i, targets.length)
    try {
      const fd = new FormData()
      fd.append('project_id', String(projectId))
      fd.append('file', file)
      fd.append('file_path', file.name)
      await uploadCodeFile(fd)
      okCount += 1
    } catch (err) {
      failures.push(`${file.name}: ${err instanceof Error ? err.message : '上传失败'}`)
    }
    setUploadProgress(file.name, i + 1, targets.length)
  }
  resetUploadProgress()
  // 若所有上传请求都失败,立即软删除刚建的空项目,不把失败项目留给用户。
  if (!okCount) {
    try { await deleteProject(projectId) } catch { /* 仍优先把真实上传失败反馈给用户 */ }
    throw new Error(`所有文件上传失败,未保留项目${failures.length ? ` (${failures[0]})` : ''}`)
  }
  // 后端逐文件上传会分别识别语言;若首个可识别源码文件更可靠,回填项目主语言。
  const uploadedLanguage = inferProjectLanguage(targets.filter((f) => !IMAGE_EXTS.has(f.name.split('.').pop()?.toLowerCase() ?? '')))
  if (uploadedLanguage !== language) {
    try { await updateProject(projectId, { language: uploadedLanguage }) } catch { /* 不影响已上传文件 */ }
  }
  const imageNote = imageCount ? `（含 ${imageCount} 张图片附件）` : ''
  const summary = `我已帮你把 ${okCount} 个文件${imageNote}上传到项目「${projectName}」(#${projectId},语言 ${uploadedLanguage})${failures.length ? `,${failures.length} 个失败(${failures[0]})` : ''}。小菱正在自动启动隔离沙箱全量验证，完成后会返回白盒、黑盒与多 Agent 审查报告。`
  messages.value.push({ id: messageId(), role: 'assistant', content: summary, time: dayjs().format('HH:mm') })
  await nextTick()
  scrollToBottom()
  if (failures.length) ElMessage.warning(`已上传 ${okCount} 个,${failures.length} 个失败`)
  else ElMessage.success(`已创建项目「${projectName}」并上传 ${okCount} 个文件`)
  // 上传成功后交给固定全量验证工具，避免模型只回复“下一步做什么”而不执行。
  await runResponse({
    action: 'start',
    surface: 'user',
    session_id: sessionId.value,
    messages: [...conversationHistory(), {
      role: 'user',
      content: buildAutoValidationPrompt(projectId, uploadedLanguage, projectName),
    }],
  })
}

function close(): void {
  // 关闭前持久化运行状态,确保重开后能识别未完成会话(运行中/等待审批/等待输入)并跳回
  saveActiveAgentChatSession('user', sessionId.value)
  persistSnapshot()
  activityStore.clear()
  emit('update:visible', false)
}

watch(() => props.visible, async (val) => {
  if (!val) return
  await nextTick()
  restoreOrAnchor()
  switcherRef.value?.ensureFreshOnOpen()
  scrollToBottom()
})

watch(() => props.prefill, (prefill) => {
  if (!prefill) return
  inputText.value = prefill
  emit('consumed-prefill')
  // 预填文本整体选中:用户直接打字即整体替换,不会与预填拼接污染首条消息
  void nextTick(() => {
    const input = chatInputRef.value
    if (input) {
      input.focus()
      input.setSelectionRange(0, input.value.length)
    }
  })
}, { immediate: true })

function showReconnectHint(): void {
  reconnectHint.value = true
  if (reconnectHintTimer !== undefined) window.clearTimeout(reconnectHintTimer)
  reconnectHintTimer = window.setTimeout(() => { reconnectHint.value = false }, 6000)
}

/** 网络恢复后立即无感接管进度,无需用户手动刷新。 */
function handleOnline(): void {
  sessionPollFailures = 0
  showReconnectHint()
  scheduleSessionPoll(true)
}

function handleOffline(): void {
  sessionPollError.value = '网络断开,恢复后自动继续'
}

/** 回到前台时立即同步一次,避免长任务被标签页冻结后出现陈旧进度。 */
function handleVisibilityChange(): void {
  if (document.visibilityState !== 'visible') return
  if (!sessionPollStopped && sessionId.value && isAgentResponseSessionOccupied(sessionRun.value?.status)) {
    scheduleSessionPoll(true)
  }
}

onBeforeUnmount(() => {
  meshBridge.stop()
  persistSnapshot()
  sessionPollStopped = true
  invalidateSessionPoll()
  clearLiveTeamPoll()
  // 组件卸载（退出登录/离开布局）只断开本地订阅。服务端任务由持久检查点继续执行；
  // 只有用户点击“停止”才走 cancelResponse() 并取消服务端运行。
  activeResponse = null
  activityStore.clear()
  window.clearTimeout(projectSearchTimer)
  window.clearTimeout(copiedResetTimer)
  if (reconnectHintTimer !== undefined) window.clearTimeout(reconnectHintTimer)
  window.removeEventListener('keydown', handleGlobalKeydown)
  window.removeEventListener('online', handleOnline)
  window.removeEventListener('offline', handleOffline)
  document.removeEventListener('visibilitychange', handleVisibilityChange)
})

onMounted(() => {
  meshBridge.start()
  window.addEventListener('keydown', handleGlobalKeydown)
  window.addEventListener('online', handleOnline)
  window.addEventListener('offline', handleOffline)
  document.addEventListener('visibilitychange', handleVisibilityChange)
})
</script>

<template>
  <Teleport to="body">
    <button
      v-if="!visible"
      class="chat-fab"
      :class="{ 'is-busy': mascotStatus !== 'idle' }"
      type="button"
      :aria-label="`打开${MASCOT_NAME}助手`"
      :title="`${MASCOT_NAME} · Prism 小助手`"
      @click="emit('update:visible', true)"
    >
      <PrismMascot :size="44" :status="mascotStatus !== 'idle' ? 'running' : 'idle'" />
    </button>
    <Transition name="drawer">
      <div v-if="visible" class="chat-overlay">
        <div
          ref="panelRef"
          class="chat-drawer"
          :class="{ 'is-dragging': dragging, 'drag-over': dragActive }"
          :style="panelStyle"
          @pointermove="moveDrag"
          @pointerup="endDrag"
          @pointercancel="endDrag"
          @dragenter.prevent="onDragEnter"
          @dragover.prevent="onDragOver"
          @dragleave.prevent="onDragLeave"
          @drop.prevent="onDrop"
        >
          <div v-if="dragActive" class="drop-mask">
            <div class="drop-mask-text">松开鼠标,把文件交给小菱建项目</div>
          </div>
          <div class="chat-header" :class="{ 'is-running': mascotStatus === 'running' }">
            <button class="panel-drag-handle" type="button" aria-label="移动 Agent 助手窗口" title="拖拽移动窗口" @pointerdown="beginDrag">⠿</button>
            <div class="chat-title">
              <span class="mascot-badge">
                <PrismMascot :size="34" :status="mascotStatus" />
              </span>
              <div class="chat-title-text">
                <div class="chat-title-line">
                  <span :title="`当前模型:${modelName}`">{{ MASCOT_NAME }} · Agent 助手</span>
                  <span class="run-badge" :class="`run-${mascotStatus}`">
                    <i></i>{{ runStatusLabel }}
                  </span>
                  <span v-if="sessionPollError" class="sync-status is-error" role="status">{{ sessionPollError }}</span>
                  <span v-else-if="reconnectHint" class="sync-status is-reconnect" role="status">网络已恢复,继续执行</span>
                  <span v-else-if="sessionLastPolledAt" class="sync-status" role="status">自动监控 · 已同步 {{ sessionLastPolledAt }}</span>
                  <button
                    v-if="canRetryRun"
                    class="retry-run-btn"
                    type="button"
                    title="从失败位置继续运行，不会重放已执行的审批操作"
                    @click="retryRun()"
                  >重试运行</button>
                </div>
                <AgentSessionSwitcher
                  ref="switcherRef"
                  class="chat-session-switch"
                  storage-key="user"
                  :legacy-key="LEGACY_SESSION_KEY"
                  id-prefix="user"
                  :welcome-text="WELCOME_TEXT"
                  :discover-remote="true"
                  @select="handleSessionSelect"
                  @sessions-changed="handleSwitcherReady"
                  @archive="handleSessionArchive"
                />
              </div>
            </div>
            <button
              class="close-btn"
              type="button"
              :aria-label="`关闭${MASCOT_NAME}助手`"
              :title="`关闭${MASCOT_NAME}助手`"
              @click="close"
            >
              <el-icon><Close /></el-icon>
            </button>
          </div>

          <div class="chat-body-region">
            <Transition name="mascot-float">
              <div v-if="messages.length <= 1 && !showTyping" class="mascot-hero" aria-hidden="true">
                <PrismMascot :size="120" :status="mascotStatus" />
              </div>
            </Transition>

            <Transition name="mascot-float">
              <div v-if="messages.length <= 1 && !showTyping" class="quick-questions" aria-label="快捷问题">
                <button
                  v-for="question in QUICK_QUESTIONS"
                  :key="question"
                  class="quick-question"
                  type="button"
                  @click="askQuickQuestion(question)"
                >
                  {{ question }}
                </button>
              </div>
            </Transition>

            <div
              v-if="showToolProgress || (mascotStatus === 'running' && lastActiveToolName)"
              class="chat-progress"
              :class="{ 'is-busy': mascotStatus === 'running' }"
              role="status"
              aria-label="小菱执行进度"
            >
              <span class="chat-progress-phrase">
                <template v-if="mascotStatus === 'running' && lastActiveToolName">
                  {{ toolRunningPhrase(lastActiveToolName) }}
                </template>
                <template v-else-if="toolStepProgress.current">
                  当前:{{ toolRunningPhrase(toolStepProgress.current.name) }}
                </template>
                <template v-else>小菱正在推进…</template>
              </span>
              <template v-if="showToolProgress">
                <FluidProgress
                  class="chat-progress-fluid"
                  :progress="toolProgressPercent"
                  :height="6"
                />
                <span class="chat-progress-count">{{ toolStepProgress.done }}/{{ toolStepProgress.total }} 步</span>
              </template>
            </div>

            <Transition name="mascot-float">
              <div v-if="sessionRestoring" class="session-restoring-hint" role="status" aria-live="polite">
                <span class="session-restoring-spinner" aria-hidden="true"></span>
                <span class="session-restoring-text">
                  正在恢复这个对话<span v-if="sessionRun?.status === 'running'">，小菱还有任务在后台跑着，马上接回进度…</span><span v-else>，从服务器拉取历史消息…</span>
                </span>
              </div>
            </Transition>

            <div ref="chatBody" class="chat-body" :class="{ 'is-restoring': sessionRestoring }" @click="onMessageClick">
            <div v-for="(msg, i) in messages" :key="msg.id ?? i" class="msg-row" :class="msg.role">
              <div class="msg-avatar">
                <template v-if="msg.role === 'user'">U</template>
                <PrismMascot v-else :size="26" :status="'idle'" />
              </div>
              <div class="msg-bubble" :class="{ 'has-response-control': msg.toolCalls?.length || msg.approval || msg.inputRequest }">
                <!-- 步骤气泡: 仅对 assistant + 有 steps 时展示;默认折叠降噪 -->
                <details
                  v-if="msg.role === 'assistant' && msg.steps && msg.steps.length"
                  class="step-stream"
                >
                  <summary class="step-summary">
                    Agent 调度链 · 共 {{ msg.steps.length }} 步
                  </summary>
                  <ol class="step-list">
                    <li
                      v-for="(s, idx) in msg.steps"
                      :key="idx"
                      class="step-item"
                      :class="`step-${s.type}`"
                    >
                      <AgentAvatar
                        :code="s.agent"
                        :status="stepStatus(s)"
                        :size="24"
                        :label="s.agent"
                      />
                      <div class="step-info">
                        <div class="step-line">
                          <span class="step-agent font-mono">{{ s.agent }}</span>
                          <span class="step-type">{{ stepLabel(s) }}</span>
                          <span class="step-time font-mono">{{ s.time }}</span>
                        </div>
                        <div class="step-msg">{{ s.message }}</div>
                      </div>
                    </li>
                  </ol>
                </details>

                <!-- v3.0 双层调度 step tree: 展示 LLM 规划的调用链 -->
                <details
                  v-if="msg.role === 'assistant' && msg.planSteps && msg.planSteps.length"
                  class="plan-tree"
                >
                  <summary class="plan-summary">
                    <el-icon class="plan-icon" aria-hidden="true"><Connection /></el-icon>
                    双层调度调用链 · LLM 规划 {{ msg.planSteps.length }} 步
                    <span class="plan-total-ms">
                      总耗时 {{ planTotalMs(msg.planSteps) }}ms
                    </span>
                  </summary>
                  <ol class="plan-list">
                    <li
                      v-for="step in msg.planSteps"
                      :key="step.step_index"
                      class="plan-step"
                      :class="{ 'plan-failed': !step.success }"
                    >
                      <div class="plan-step-head">
                        <span class="plan-step-idx">#{{ step.step_index + 1 }}</span>
                        <code class="plan-tool">{{ step.tool_name }}</code>
                        <span
                          class="plan-step-status"
                          :class="step.success ? 'plan-ok' : 'plan-bad'"
                        >
                          {{ step.success ? '✓' : '✗' }}
                        </span>
                        <span class="plan-step-ms">{{ step.duration_ms }}ms</span>
                      </div>
                      <p v-if="step.reason" class="plan-reason">{{ step.reason }}</p>
                      <details v-if="step.arguments && Object.keys(step.arguments).length" class="plan-args">
                        <summary>参数 ({{ Object.keys(step.arguments).length }})</summary>
                        <pre class="plan-json">{{ JSON.stringify(step.arguments, null, 2) }}</pre>
                      </details>
                      <details v-if="step.data_preview" class="plan-preview">
                        <summary>输出预览</summary>
                        <pre class="plan-json">{{ step.data_preview }}</pre>
                      </details>
                      <p v-if="!step.success && step.error" class="plan-error">
                        {{ step.error }}
                      </p>
                    </li>
                  </ol>
                </details>

                <!-- 错误卡片:失败/取消留在消息流,带重试与新建对话 -->
                <div v-if="msg.role === 'error'" class="msg-error-card">
                  <div class="msg-error-head">
                    <el-icon aria-hidden="true"><CircleCloseFilled /></el-icon>
                    <span>这次没有完成</span>
                  </div>
                  <p class="msg-error-text">{{ msg.content }}</p>
                  <div class="msg-error-actions">
                    <button
                      v-if="msg.errorCard?.retryable"
                      class="msg-error-btn is-retry"
                      type="button"
                      :disabled="!canStartRun"
                      @click="retryLastAction()"
                    >重试</button>
                    <button class="msg-error-btn" type="button" @click="startNewChat()">新建对话</button>
                  </div>
                </div>

                <div
                  v-if="msg.role === 'assistant' && msg.content"
                  class="msg-content markdown-body"
                  v-html="renderMarkdown(msg.content)"
                />
                <div v-else-if="msg.role === 'user'" class="msg-content">{{ msg.content }}</div>
                <!-- 助手消息 hover 显示复制按钮 -->
                <button
                  v-if="msg.role === 'assistant' && msg.content"
                  class="msg-copy-btn"
                  :class="{ 'is-copied': copiedMessageId === msg.id }"
                  type="button"
                  :title="copiedMessageId === msg.id ? '已复制' : '复制这条回复'"
                  :aria-label="copiedMessageId === msg.id ? '已复制' : '复制这条回复'"
                  @click="copyMessage(msg)"
                >
                  <el-icon v-if="copiedMessageId === msg.id" aria-hidden="true"><Check /></el-icon>
                  <el-icon v-else aria-hidden="true"><CopyDocument /></el-icon>
                </button>

                <!-- 页面引导:模型约定路由 + 指令导航按钮,鉴权由 AgentNavLink 同源守卫裁决 -->
                <div v-if="msg.role === 'assistant' && msg.navigations?.length" class="nav-directives">
                  <AgentNavLink
                    v-for="nav in msg.navigations"
                    :key="nav.route"
                    :href="nav.route"
                    :label="nav.label || '前往对应页面'"
                    :hint="nav.hint"
                    prominent
                  />
                </div>

                <ResponseToolTimeline
                  v-if="msg.toolCalls?.length || msg.auditPhases?.length"
                  :calls="msg.toolCalls ?? []"
                  :audit-phases="msg.auditPhases"
                />

                <!-- 团队卡片属于调用时间线,随消息锚点出现,不会在最终结论后统一补充。 -->
                <template
                  v-for="(teamId, teamIndex) in msg.teamIds ?? []"
                  :key="`team-${teamId}`"
                >
                  <AgentTeamTrace
                    v-if="teamById(teamId)"
                    :team="teamById(teamId) ?? null"
                    :loading="agentTeamLoading"
                    :error="teamIndex === 0 ? agentTeamError : ''"
                    @open-detail="openTeamWindow"
                  />
                </template>

                <ResponseApprovalCard
                  v-if="msg.approval"
                  :approval="msg.approval"
                  :loading="loading"
                  @decide="decideApproval(msg, $event)"
                />

                <ResponseInputCard
                  v-if="msg.inputRequest"
                  :request="msg.inputRequest"
                  :loading="loading"
                  @update:answer="msg.inputRequest.answer = $event"
                  @submit="submitInput(msg, $event)"
                />

                <!-- Clarify 主动追问表单 -->
                <div
                  v-if="msg.clarify"
                  class="clarify-card"
                  :class="{ 'is-danger': confirmationQuestion(msg)?.type === 'danger_confirm' }"
                >
                  <header class="clarify-head">
                    <el-icon class="clarify-icon">
                      <WarningFilled v-if="confirmationQuestion(msg)?.type === 'danger_confirm'" />
                      <CircleCheck v-else />
                    </el-icon>
                    <span>{{ confirmationQuestion(msg) ? '请确认本次操作' : '请补充以下信息,我再继续执行' }}</span>
                  </header>
                  <div
                    v-for="q in msg.clarify.questions"
                    :key="q.key"
                    class="clarify-field"
                  >
                    <label class="clarify-label">
                      {{ q.label }}
                      <span v-if="q.hint" class="clarify-hint">{{ q.hint }}</span>
                    </label>
                    <el-input
                      v-if="q.type === 'text'"
                      v-model="clarifyAnswers[msg.clarify.clarify_id][q.key] as string"
                      size="small"
                    />
                    <el-input
                      v-else-if="q.type === 'textarea' || q.type === 'code'"
                      v-model="clarifyAnswers[msg.clarify.clarify_id][q.key] as string"
                      type="textarea"
                      :rows="4"
                      size="small"
                    />
                    <el-input-number
                      v-else-if="q.type === 'number'"
                      v-model="clarifyAnswers[msg.clarify.clarify_id][q.key] as number"
                      size="small"
                    />
                    <el-select
                      v-else-if="q.type === 'select'"
                      v-model="clarifyAnswers[msg.clarify.clarify_id][q.key]"
                      size="small"
                      popper-class="agent-clarify-popper"
                      style="width: 100%"
                    >
                      <el-option
                        v-for="opt in q.options ?? []"
                        :key="String(opt.value)"
                        :label="opt.label"
                        :value="opt.value"
                      />
                    </el-select>
                    <el-select
                      v-else-if="q.type === 'select_project'"
                      v-model="clarifyAnswers[msg.clarify.clarify_id][q.key]"
                      size="small"
                      filterable
                      remote
                      reserve-keyword
                      default-first-option
                      :remote-method="searchProjectOptions"
                      :loading="projectOptionsLoading"
                      popper-class="agent-clarify-popper"
                      placeholder="搜索并选择项目"
                      style="width: 100%"
                    >
                      <el-option
                        v-for="opt in clarifyProjectOptions(q)"
                        :key="String(opt.value)"
                        :label="opt.label"
                        :value="opt.value"
                      />
                    </el-select>
                    <el-input
                      v-if="q.type === 'select_project' && isCustomProjectSelected(msg.clarify.clarify_id, q.key)"
                      v-model="clarifyCustomProjectInputs[msg.clarify.clarify_id][q.key]"
                      class="clarify-custom-input"
                      size="small"
                      clearable
                      placeholder="输入项目名称或项目 ID"
                    />
                    <el-select
                      v-else-if="q.type === 'select_task'"
                      v-model="clarifyAnswers[msg.clarify.clarify_id][q.key]"
                      size="small"
                      filterable
                      allow-create
                      default-first-option
                      popper-class="agent-clarify-popper"
                      placeholder="选择或输入任务 ID"
                      style="width: 100%"
                    >
                      <el-option
                        v-for="opt in (q.options && q.options.length ? q.options : taskOptions)"
                        :key="String(opt.value)"
                        :label="opt.label"
                        :value="opt.value"
                      />
                    </el-select>
                  </div>
                  <footer class="clarify-foot">
                    <template v-if="confirmationQuestion(msg)">
                      <el-button
                        size="small"
                        :disabled="loading"
                        @click="submitConfirmation(msg, '取消')"
                      >
                        取消
                      </el-button>
                      <el-button
                        size="small"
                        :type="confirmationQuestion(msg)?.type === 'danger_confirm' ? 'danger' : 'primary'"
                        :loading="loading"
                        @click="submitConfirmation(msg, '确认')"
                      >
                        {{ confirmationQuestion(msg)?.type === 'danger_confirm' ? '执行' : '批准' }}
                      </el-button>
                    </template>
                    <el-button
                      v-else
                      size="small"
                      type="primary"
                      :loading="loading"
                      @click="submitClarify(msg)"
                    >
                      提交并继续
                    </el-button>
                  </footer>
                </div>

                <div class="msg-time">{{ msg.time }}</div>
              </div>
            </div>

            <AgentTeamTrace
              v-for="(team, index) in unanchoredAgentTeams"
              :key="`unanchored-team-${team.team_id}`"
              :team="team"
              :loading="agentTeamLoading"
              :error="index === 0 ? agentTeamError : ''"
              @open-detail="openTeamWindow"
            />

            <div v-if="showTyping" class="msg-row assistant">
              <div class="msg-avatar">
                <PrismMascot :size="26" :status="'running'" />
              </div>
              <div class="msg-bubble typing" aria-label="小菱正在思考">
                <AiOrb :size="28" state="thinking" :halo="false" />
                <span class="typing-label">小菱正在想</span>
                <span class="typing-dot" />
                <span class="typing-dot" />
                <span class="typing-dot" />
              </div>
            </div>
            </div>
          </div>

          <div class="chat-input-area">
            <div v-if="uploadStatusVisible" class="upload-status" role="status" aria-label="上传进度">
              <span class="upload-status-text" :title="uploadProgress.phase">{{ uploadProgress.phase }}</span>
              <FluidProgress
                class="upload-status-fluid"
                :progress="uploadPercent"
                :height="8"
              />
              <span class="upload-status-count">{{ uploadProgress.completed }}/{{ uploadProgress.total }} 个文件</span>
            </div>
            <p v-else class="chat-input-hint">支持直接拖入代码文件帮你建项目;Shift+Enter 换行</p>
            <input
              ref="uploadInput"
              class="source-upload-input"
              type="file"
              multiple
              accept=".py,.js,.jsx,.ts,.tsx,.vue,.html,.css,.java,.go,.php,.rb,.c,.h,.cpp,.cc,.cs,.rs,.json,.yaml,.yml,.toml,.md,.txt,.png,.jpg,.jpeg,.gif,.webp,.bmp,.svg"
              aria-label="选择源码文件"
              @change="onFileInput"
            />
            <textarea
              ref="chatInputRef"
              v-model="inputText"
              class="chat-input"
              :placeholder="sessionRestoring ? '正在恢复 Agent 会话' : sessionBusy ? (isAgentResponseSessionWaiting(sessionRun?.status) ? '请先处理上方待办(审批/追问),或点击 + 新建对话' : '小菱正在运行中…可点击 + 新建对话并行处理') : '输入问题,Enter 发送'"
              rows="2"
              :disabled="loading || uploading || sessionRestoring || sessionBusy"
              @keydown="handleKeydown"
            />
            <div class="chat-input-actions">
              <button
                class="upload-btn"
                type="button"
                :disabled="loading || uploading || sessionRestoring || sessionBusy"
                title="选择源码文件或图片附件"
                @click="uploadInput?.click()"
              >
                <el-icon aria-hidden="true"><Upload /></el-icon>
                选择源码
              </button>
              <button
                v-if="canStopResponse"
                class="stop-btn"
                type="button"
                title="停止小菱本次回答"
                @click="requestStopResponse"
              >
                <span class="stop-btn-icon" aria-hidden="true"></span>
                停止响应
              </button>
              <button
                v-else
                class="send-btn"
                type="button"
                :disabled="!canSend"
                @click="sendMessage"
              >
                发送
              </button>
            </div>
          </div>
        </div>
      </div>
    </Transition>
    <AgentTeamWindow
      v-model:visible="teamWindowVisible"
      :team="teamWindowTeam"
      @refreshed="void refreshAgentTeam()"
      @ask-member="handleAskMember"
    />
    <TaskCancelConfirm :visible="cancelPromptVisible" @confirm="handleCancelConfirm" @dismiss="cancelPromptVisible = false" />
  </Teleport>
</template>

<style scoped>
.chat-overlay {
  position: fixed;
  right: 24px;
  bottom: 24px;
  z-index: 3000;
}

.chat-drawer {
  position: fixed;
  width: min(400px, calc(100vw - 32px));
  height: min(620px, calc(100dvh - 48px));
  background: #fff;
  display: flex;
  flex-direction: column;
  border: 1px solid rgba(91, 88, 232, 0.14);
  border-radius: 18px;
  box-shadow:
    0 18px 48px rgba(51, 48, 140, 0.16),
    0 4px 14px rgba(15, 18, 34, 0.08);
  overflow: hidden;
}

.chat-drawer.drag-over {
  border-color: var(--brand-500, #5b58e8);
  border-style: dashed;
  border-width: 2px;
}

.drop-mask {
  position: absolute;
  inset: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(91, 88, 232, 0.08);
  backdrop-filter: blur(2px);
  pointer-events: none;
}

.drop-mask-text {
  padding: 14px 22px;
  border-radius: 12px;
  background: var(--brand-50, #f5f6ff);
  border: 1.5px dashed var(--brand-400, #8f8cf0);
  color: var(--brand-600, #5b58e8);
  font-size: 14px;
  font-weight: 600;
}

.chat-fab {
  position: fixed; right: 24px; bottom: 24px; z-index: 3000;
  width: 60px; height: 60px; border: 0; border-radius: 50%;
  background: linear-gradient(145deg, #ffffff, #eef0fb);
  display: grid; place-items: center;
  cursor: pointer;
  box-shadow: 0 8px 24px rgba(91, 88, 232, 0.28), 0 2px 6px rgba(15, 18, 34, 0.1);
  transition: transform .16s ease, box-shadow .16s ease;
}
.chat-fab:hover { transform: scale(1.06) translateY(-2px); box-shadow: 0 12px 30px rgba(91, 88, 232, 0.36), 0 3px 8px rgba(15, 18, 34, 0.12); }
.chat-fab.is-busy::after {
  content: '';
  position: absolute;
  inset: -3px;
  border-radius: 50%;
  border: 2px solid transparent;
  border-top-color: var(--brand-500);
  border-right-color: var(--accent-400);
  animation: fab-spin 1.2s linear infinite;
}
@keyframes fab-spin { to { transform: rotate(360deg); } }

.mascot-badge {
  flex-shrink: 0;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: linear-gradient(145deg, #f2f3ff, #e4ecfb);
  box-shadow: inset 0 0 0 1px rgba(91, 88, 232, 0.16);
  overflow: hidden;
}

.chat-title-text {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.chat-title-line {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.chat-session-switch {
  align-self: flex-start;
}

.retry-run-btn {
  margin-left: 6px;
  padding: 1px 8px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  background: #fff;
  color: var(--color-primary);
  font-size: 11px;
  line-height: 18px;
  cursor: pointer;
}
.retry-run-btn:hover { border-color: var(--color-primary); background: #eef5ff; }
.run-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 10px;
  font-weight: 500;
  padding: 1px 7px;
  border-radius: 999px;
  border: 1px solid transparent;
}
.run-badge i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
.run-idle { color: var(--gray-500); background: var(--gray-100); border-color: var(--gray-200, #e5e6eb); }
.run-running { color: #2f7a3d; background: rgba(79, 184, 122, 0.12); border-color: rgba(79, 184, 122, 0.35); }
.run-running i { animation: run-blink 1s ease-in-out infinite; }
.run-waiting { color: #b68039; background: rgba(217, 168, 87, 0.16); border-color: rgba(217, 168, 87, 0.4); }
.run-waiting i { animation: run-blink 1s ease-in-out infinite; }
.sync-status { color: var(--color-text-secondary); font-size: 9.5px; white-space: nowrap; }
.sync-status.is-error { color: #c43d36; font-weight: 600; }
.sync-status.is-reconnect { color: #2e9e5b; font-weight: 600; }
@keyframes run-blink { 0%, 100% { opacity: 0.35; } 50% { opacity: 1; } }

.chat-progress {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 20px;
  font-size: 11px;
  color: var(--color-text-secondary);
  background: linear-gradient(90deg, rgba(91, 88, 232, 0.06), rgba(61, 188, 217, 0.06));
  border-bottom: 1px solid var(--color-border-light);
  overflow: hidden;
}
.chat-progress-phrase {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chat-progress-count {
  flex: none;
  color: var(--brand-600);
  font-weight: 600;
  font-size: 10.5px;
  white-space: nowrap;
}
.chat-progress code {
  font-family: var(--font-mono, monospace);
  color: var(--brand-600);
  font-size: 10.5px;
  background: rgba(91, 88, 232, 0.1);
  padding: 0 5px;
  border-radius: 3px;
}

.chat-drawer.is-dragging { user-select: none; }
.panel-drag-handle { display: grid; place-items: center; flex: 0 0 auto; width: 24px; height: 30px; margin-left: -8px; border: 0; border-radius: 6px; background: transparent; color: var(--color-text-placeholder, #8f959e); font-size: 18px; line-height: 1; cursor: grab; touch-action: none; }
.panel-drag-handle:hover { color: var(--primary-color, #5b58e8); background: rgba(91, 88, 232, .08); }
.chat-drawer.is-dragging .panel-drag-handle { cursor: grabbing; }
.chat-header {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--color-border-light);
  background: linear-gradient(135deg, rgba(91, 88, 232, 0.07), rgba(61, 188, 217, 0.06) 70%, transparent);
  flex-shrink: 0;
}
/* 运行中:头部底边泛起品牌色流动光线,一眼知道小菱在工作(尼尔森·状态可见) */
.chat-header::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: -1px;
  height: 2px;
  background: linear-gradient(90deg, transparent, var(--brand-400), var(--accent-400), transparent);
  background-size: 200% 100%;
  opacity: 0;
  transition: opacity 0.3s ease;
}
.chat-header.is-running::after {
  opacity: 1;
  animation: chat-header-flow 2.2s linear infinite;
}
@keyframes chat-header-flow {
  0% { background-position: 100% 0; }
  100% { background-position: -100% 0; }
}

.chat-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
  font-weight: 600;
  color: var(--color-text-primary);
}

.title-icon {
  color: var(--brand-500);
  font-size: 18px;
}

.close-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 16px;
  transition: all var(--transition-fast);
}

.close-btn:hover {
  background: var(--gray-100);
  color: var(--color-text-primary);
}

.chat-body-region {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.mascot-hero {
  position: absolute;
  top: 6px;
  right: 4px;
  z-index: 1;
  pointer-events: none;
  opacity: 0.95;
  /* 吉祥物周围的光谱环境光晕,营造「AI 在场」的氛围 */
  filter: drop-shadow(0 8px 24px rgba(91, 88, 232, 0.22)) drop-shadow(0 2px 8px rgba(61, 188, 217, 0.14));
}

.quick-questions {
  position: absolute;
  top: 134px;
  right: 8px;
  z-index: 1;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--sp-2);
}

.quick-question {
  border: 1px solid var(--brand-200);
  border-radius: 999px;
  /* 玻璃拟态白胶囊:半透渐变 + 内高光 */
  background: linear-gradient(135deg, rgba(255, 255, 255, 0.92), rgba(239, 238, 254, 0.78));
  color: var(--brand-700);
  font-size: var(--fs-xs);
  min-height: 36px;
  padding: 8px var(--sp-4);
  cursor: pointer;
  box-shadow: 0 2px 8px rgba(91, 88, 232, 0.10), inset 0 1px 0 rgba(255, 255, 255, 0.7);
  transition: all var(--transition-fast);
  white-space: nowrap;
  /* 逐个错峰入场 */
  animation: quick-question-in 0.4s cubic-bezier(0.16, 0.84, 0.44, 1) backwards;
}
.quick-question:nth-child(2) { animation-delay: 60ms; }
.quick-question:nth-child(3) { animation-delay: 120ms; }

@keyframes quick-question-in {
  from { opacity: 0; transform: translateX(10px); }
  to   { opacity: 1; transform: translateX(0); }
}

.quick-question:hover {
  background: linear-gradient(135deg, #ffffff, var(--brand-100));
  border-color: var(--brand-300);
  transform: translateY(-1px);
  box-shadow: 0 4px 14px rgba(91, 88, 232, 0.18), inset 0 1px 0 rgba(255, 255, 255, 0.8);
}

@media (prefers-reduced-motion: reduce) {
  .quick-question { transition: none; animation: none; }
  .quick-question:hover { transform: none; }
  .msg-copy-btn { transition: none; }
  .typing-label { animation: none; }
  .typing-dot { animation: none; opacity: 1; transform: none; }
  .msg-bubble { animation: none; }
  .msg-row.assistant .msg-bubble { transition: none; }
  .msg-row.assistant .msg-bubble:hover { transform: none; }
  .chat-header.is-running::after { animation: none; opacity: 0.6; }
  .stop-btn { transition: none; }
  .run-running i, .run-waiting i { animation: none; opacity: 1; }
  .send-btn { transition: none; }
  .send-btn:hover:not(:disabled) { transform: none; }
  .chat-input { transition: none; }
}

.mascot-float-enter-active,
.mascot-float-leave-active { transition: opacity 0.35s ease, transform 0.35s ease; }
.mascot-float-enter-from,
.mascot-float-leave-to { opacity: 0; transform: translateY(10px) scale(0.9); }

.session-restoring-hint {
  flex: none;
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px 16px 0;
  padding: 8px 12px;
  border: 1px dashed var(--brand-200);
  border-radius: 10px;
  background: var(--brand-50);
  color: var(--brand-700);
  font-size: 12px;
}
.session-restoring-spinner {
  flex: none;
  width: 13px;
  height: 13px;
  border: 2px solid var(--brand-200);
  border-top-color: var(--brand-500);
  border-radius: 50%;
  animation: session-restoring-spin 0.9s linear infinite;
}
@keyframes session-restoring-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) {
  .session-restoring-spinner { animation: none; }
}

.chat-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  /* 极淡品牌氛围底:斜向紫青渐变 + 顶部光晕,替代纯白贯穿 */
  background:
    radial-gradient(circle at 85% 0%, rgba(142, 136, 245, 0.10), transparent 42%),
    radial-gradient(circle at 8% 100%, rgba(61, 188, 217, 0.07), transparent 46%),
    linear-gradient(180deg, #fbfbfe 0%, #f7f8fc 100%);
  /* 滚动内容在顶/底部轻微柔化(8px),不切文字与阴影 */
  -webkit-mask-image: linear-gradient(180deg, transparent 0, #000 8px, #000 calc(100% - 8px), transparent 100%);
  mask-image: linear-gradient(180deg, transparent 0, #000 8px, #000 calc(100% - 8px), transparent 100%);
}

/* 恢复历史时整片渲染,跳过入场动画避免整屏闪烁 */
.chat-body.is-restoring .msg-bubble { animation: none; }

.msg-row {
  display: flex;
  gap: 10px;
  max-width: 100%;
}

.msg-row.user { flex-direction: row-reverse; }

.msg-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 600;
  flex-shrink: 0;
  overflow: hidden;
}

.msg-row.user .msg-avatar { background: var(--brand-500); color: #fff; }
.msg-row.assistant .msg-avatar {
  background: linear-gradient(145deg, #f2f3ff, #e4ecfb);
  box-shadow: inset 0 0 0 1px rgba(91, 88, 232, 0.16);
}

.msg-bubble {
  position: relative;
  max-width: calc(100% - 48px);
  padding: 10px 14px;
  border-radius: 12px;
  font-size: 13.5px;
  line-height: 1.6;
  animation: msg-bubble-in 0.24s ease-out;
}

@keyframes msg-bubble-in {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: translateY(0); }
}

.msg-bubble.has-response-control { width: calc(100% - 48px); }

/* 用户气泡深底:时间戳用半透明白 */
.msg-row.user .msg-time { color: rgba(255, 255, 255, 0.75); }

/* hover 复制按钮:默认淡出,气泡悬停/键盘聚焦时可见 */
.msg-copy-btn {
  position: absolute;
  top: 4px;
  right: 4px;
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: 1px solid var(--color-border-light);
  border-radius: 6px;
  background: var(--color-bg-card);
  color: var(--color-text-secondary);
  font-size: 13px;
  cursor: pointer;
  opacity: 0;
  transition: opacity var(--transition-fast), color var(--transition-fast), border-color var(--transition-fast);
}

.msg-bubble:hover .msg-copy-btn,
.msg-copy-btn:focus-visible { opacity: 1; }
.msg-copy-btn:hover { color: var(--brand-600); border-color: var(--brand-300); }
.msg-copy-btn.is-copied { color: var(--color-success); border-color: var(--color-success); opacity: 1; }

/* 错误卡片:红色左边条,失败/取消留在消息流里 */
.msg-row.error .msg-avatar { background: var(--color-danger-light); box-shadow: inset 0 0 0 1px rgba(220, 73, 97, 0.25); }

.msg-error-card {
  padding: 2px 0 2px 10px;
  border-left: 3px solid var(--color-danger);
}

.msg-error-head {
  display: flex;
  align-items: center;
  gap: 5px;
  color: var(--color-danger);
  font-weight: 600;
  font-size: 12.5px;
}

.msg-error-text {
  margin: 5px 0 0;
  color: var(--color-text-regular);
  font-size: 12.5px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
}

.msg-error-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

.msg-error-btn {
  min-height: 28px;
  padding: 0 12px;
  border: 1px solid var(--color-border-base);
  border-radius: 6px;
  background: var(--color-bg-card);
  color: var(--color-text-regular);
  font-size: 12px;
  cursor: pointer;
  transition: all var(--transition-fast);
}

.msg-error-btn:hover:not(:disabled) { border-color: var(--brand-400); color: var(--brand-600); }
.msg-error-btn.is-retry { border-color: var(--brand-500); color: #fff; background: var(--brand-500); }
.msg-error-btn.is-retry:hover:not(:disabled) { border-color: var(--brand-600); background: var(--brand-600); color: #fff; }
.msg-error-btn:disabled { opacity: 0.5; cursor: not-allowed; }

.msg-row.user .msg-bubble {
  background: linear-gradient(135deg, var(--brand-500), var(--brand-600));
  border: 1px solid var(--brand-600);
  border-top-right-radius: 4px;
  color: #fff;
  box-shadow: 0 3px 10px rgba(91, 88, 232, 0.22);
}

.msg-row.assistant .msg-bubble {
  /* 极浅品牌渐变底 + 光谱发丝线,替代纯白平涂,与用户气泡的渐变质感呼应 */
  background: linear-gradient(160deg, #ffffff 0%, #f7f7ff 55%, #f2f6ff 100%);
  border: 1px solid var(--gray-200);
  border-top-left-radius: 4px;
  box-shadow: 0 2px 10px rgba(91, 88, 232, 0.06), 0 1px 3px rgba(15, 23, 42, 0.05);
  overflow: hidden;
  transition: box-shadow var(--transition-base), transform var(--transition-base);
}

/* 顶部光谱发丝线:贴合气泡顶边,overflow:hidden 裁出圆角,hover 时显色 */
.msg-row.assistant .msg-bubble::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 2px;
  background: linear-gradient(90deg,
    var(--brand-400), var(--accent-400), var(--brand-300));
  opacity: 0.35;
  transition: opacity var(--transition-base);
  pointer-events: none;
}

.msg-row.assistant .msg-bubble:hover {
  box-shadow: 0 6px 18px rgba(91, 88, 232, 0.12), 0 2px 6px rgba(15, 23, 42, 0.06);
  transform: translateY(-1px);
}

.msg-row.assistant .msg-bubble:hover::before {
  opacity: 0.85;
}

.msg-content {
  white-space: pre-wrap;
  word-break: break-word;
}

.msg-content.markdown-body { white-space: normal; }
.msg-content.markdown-body :deep(p)        { margin: 0; }
.msg-content.markdown-body :deep(pre) {
  background: #1e1e2e; color: #cdd6f4;
  border-radius: 6px; padding: 12px;
  overflow-x: auto; font-size: 12px; line-height: 1.5; margin: 8px 0;
}
.msg-content.markdown-body :deep(code) {
  font-family: 'JetBrains Mono', 'Fira Code', monospace; font-size: 12px;
}
.msg-content.markdown-body :deep(p code) {
  background: var(--gray-100); padding: 1px 5px;
  border-radius: 3px; color: var(--brand-600);
}
.msg-content.markdown-body :deep(ul) { list-style: none; padding-left: 0; margin: 0; }
.msg-content.markdown-body :deep(ol) { padding-left: 18px; margin: 4px 0; }
.msg-content.markdown-body :deep(li) { margin: 2px 0; }

/* 站内页面引导链接:渲染成品牌色导航样式,未授权目标已由守卫隐藏 */
.msg-content.markdown-body :deep(a) {
  color: var(--brand-600, #5b58e8);
  font-weight: 600;
  text-decoration: none;
  border-bottom: 1px dashed var(--brand-300, #b7bcf5);
  cursor: pointer;
}
.msg-content.markdown-body :deep(a:hover) {
  color: var(--brand-500, #5b58e8);
  border-bottom-style: solid;
}

.nav-directives {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.msg-time {
  font-size: 10px;
  color: var(--color-text-placeholder);
  margin-top: 4px;
  text-align: right;
}

.msg-bubble.typing {
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 10px 16px 10px 12px;

  /* AI 思考球与文字行对齐 */
  .ai-orb {
    flex: none;
  }
}

/* 「小菱正在想」标签:跟三点动画同色呼吸,状态拟人化 */
.typing-label {
  margin-right: 6px;
  color: var(--gray-500);
  font-size: 11.5px;
  animation: run-blink 1.6s ease-in-out infinite;
}

.typing-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--color-text-placeholder);
  animation: typing 1.4s infinite;
}
.typing-dot:nth-child(2) { animation-delay: 0.2s; }
.typing-dot:nth-child(3) { animation-delay: 0.4s; }

@keyframes typing {
  0%, 60%, 100% { opacity: 0.3; transform: translateY(0); }
  30% { opacity: 1; transform: translateY(-4px); }
}

/* === StepStream === */
.step-stream {
  margin: -4px -6px 8px;
  padding: 8px 10px;
  background: #fff;
  border: 1px solid var(--color-border-light);
  border-radius: 8px;
  font-size: 12px;
}

/* v3.0 双层调度 step tree 样式 */
.plan-tree {
  margin: -4px -6px 8px;
  padding: 10px 12px;
  background: linear-gradient(135deg, rgba(91, 88, 232, 0.04), rgba(43, 191, 185, 0.04));
  border: 1px solid rgba(91, 88, 232, 0.18);
  border-radius: 8px;
  font-size: 12px;
}

.plan-summary {
  cursor: pointer;
  font-weight: 600;
  color: var(--brand-600, #5B58E8);
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.plan-icon { font-size: 14px; color: var(--brand-500); }

.plan-total-ms {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--gray-500, #909399);
  font-weight: 400;
}

.plan-list {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.plan-step {
  padding: 8px 10px;
  background: #fff;
  border: 1px solid var(--color-border-light, #EEF0F4);
  border-radius: 6px;
  border-left: 3px solid #2f9e44;

  &.plan-failed {
    border-left-color: #e5484d;
    background: rgba(229, 72, 77, 0.04);
  }
}

.plan-step-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11.5px;
}

.plan-step-idx {
  font-weight: 600;
  color: var(--gray-500, #909399);
  font-size: 10.5px;
}

.plan-tool {
  font-family: var(--font-mono, monospace);
  color: var(--brand-600, #5B58E8);
  font-size: 11px;
  background: rgba(91, 88, 232, 0.08);
  padding: 1px 6px;
  border-radius: 3px;
}

.plan-step-status {
  font-weight: 700;
  font-size: 12px;

  &.plan-ok { color: #2f9e44; }
  &.plan-bad { color: #e5484d; }
}

.plan-step-ms {
  margin-left: auto;
  font-size: 10.5px;
  color: var(--gray-500, #909399);
  font-family: var(--font-mono, monospace);
}

.plan-reason {
  margin: 4px 0 0;
  font-size: 11.5px;
  color: var(--gray-600, #606266);
  line-height: 1.5;
}

.plan-args,
.plan-preview {
  margin-top: 4px;
  font-size: 11px;

  summary {
    cursor: pointer;
    color: var(--gray-500, #909399);
    font-size: 10.5px;
  }
}

.plan-json {
  margin: 4px 0 0;
  padding: 6px 8px;
  background: #f6f7f9;
  border-radius: 4px;
  font-family: var(--font-mono, monospace);
  font-size: 10.5px;
  color: var(--gray-700, #303133);
  overflow-x: auto;
  max-height: 120px;
  overflow-y: auto;
}

.plan-error {
  margin: 4px 0 0;
  font-size: 11px;
  color: #e5484d;
  word-break: break-all;
}

.step-summary {
  cursor: pointer;
  font-weight: 600;
  color: var(--gray-700);
  margin-bottom: 4px;
  font-size: 12px;
}

.step-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.step-item {
  display: grid;
  grid-template-columns: 24px 1fr;
  align-items: center;
  gap: 8px;
}

.step-info { min-width: 0; }

.step-line {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
}

.step-agent {
  color: var(--gray-700);
  font-weight: 600;
}

.step-type {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--gray-100);
  color: var(--gray-600);
}

.step-time {
  margin-left: auto;
  color: var(--gray-400);
  font-size: 10px;
}

.step-msg {
  font-size: 11px;
  color: var(--gray-600);
  margin-top: 1px;
  line-height: 1.4;
}

.step-complete .step-type { background: rgba(79, 184, 122, 0.12); color: #4FB87A; }
.step-failed .step-type   { background: rgba(220, 73, 97, 0.12); color: #DC4961; }
.step-clarify .step-type  { background: rgba(217, 168, 87, 0.18); color: #B68039; }
.step-dispatch .step-type { background: rgba(91, 88, 232, 0.12); color: #5B58E8; }

/* === Clarify 表单 === */
.clarify-card {
  margin-top: 10px;
  border: 1px dashed #D9A857;
  background: #FFFBF0;
  border-radius: 8px;
  padding: 12px 14px;
}

.clarify-card.is-danger {
  border-style: solid;
  border-color: #D54941;
  background: #FFF6F5;
}

.clarify-card.is-danger .clarify-head {
  color: #B42318;
}

.clarify-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: 13px;
  color: #8B6A2F;
  margin-bottom: 10px;
}

.clarify-icon {
  font-size: 14px;
}

.clarify-field {
  margin-bottom: 10px;
}

.clarify-label {
  display: block;
  font-size: 12px;
  color: var(--gray-700);
  margin-bottom: 4px;
}

.clarify-hint {
  display: inline-block;
  margin-left: 6px;
  font-size: 10.5px;
  color: var(--gray-500);
}

.clarify-custom-input {
  margin-top: 8px;
}

:global(.agent-clarify-popper) {
  z-index: 3100 !important;
}

.clarify-foot {
  display: flex;
  justify-content: flex-end;
  margin-top: 6px;
  gap: 8px;
}

.response-control-card {
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--color-border-light);
  border-radius: 8px;
  background: #fff;
}

.response-control-card.is-danger { border-color: #d54941; }
.response-control-head { display: flex; align-items: center; gap: 6px; font-weight: 600; }
.response-control-card p { margin: 7px 0; color: var(--gray-600); }
.response-tool-name { display: block; overflow-wrap: anywhere; color: var(--brand-600); }
.response-control-actions { display: flex; gap: 8px; margin-top: 10px; }
.response-control-actions button,
.response-answer-submit {
  min-width: 64px;
  min-height: 32px;
  padding: 0 12px;
  border-radius: 6px;
  cursor: pointer;
}
.response-approve,
.response-answer-submit { border: 1px solid var(--brand-500); color: #fff; background: var(--brand-500); }
.response-reject { border: 1px solid var(--color-border-light); color: var(--gray-700); background: #fff; }
.response-control-actions button:disabled,
.response-answer-submit:disabled { opacity: .5; cursor: not-allowed; }
.response-question { display: block; margin-bottom: 8px; color: var(--gray-700); font-weight: 600; }
.response-answer {
  width: 100%;
  resize: vertical;
  padding: 8px 10px;
  border: 1px solid var(--color-border-light);
  border-radius: 6px;
  font: inherit;
}
.response-answer-submit { margin-top: 8px; }
.response-control-result { margin-top: 9px; color: var(--gray-600); font-size: 12px; }

.chat-input-area {
  display: flex;
  gap: 10px;
  padding: 12px 20px;
  border-top: 1px solid var(--color-border-light);
  flex-shrink: 0;
  flex-wrap: wrap;
}

.upload-status {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  margin-bottom: 2px;
  border-radius: 8px;
  background: var(--brand-50, #f5f6ff);
  border: 1px solid var(--brand-200, #d4d2f8);
  font-size: 12.5px;
  color: var(--brand-600, #5b58e8);
}

.upload-status-text {
  flex: none;
  max-width: 45%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 流体进度:取代原 CSS 渐变轨道 */
.upload-status-fluid {
  flex: 1 1 60px;
  min-width: 48px;
}

.chat-progress-fluid {
  flex: 1 1 72px;
  min-width: 48px;
  max-width: 160px;
}

.upload-status-count {
  flex: none;
  font-size: 10.5px;
  font-weight: 600;
  color: var(--brand-600, #5b58e8);
  white-space: nowrap;
}

.chat-input-hint {
  width: 100%;
  margin: 0 0 2px;
  color: var(--color-text-placeholder);
  font-size: 10.5px;
  line-height: 1.4;
}

.chat-input {
  flex: 1;
  border: 1px solid var(--color-border-light);
  border-radius: 10px;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.5;
  resize: none;
  outline: none;
  font-family: inherit;
  background: rgba(255, 255, 255, 0.85);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast), background var(--transition-fast);
}

/* focus 时品牌紫光晕,替代仅换边色 */
.chat-input:focus {
  border-color: var(--brand-400);
  background: #fff;
  box-shadow: 0 0 0 3px rgba(91, 88, 232, 0.12), 0 2px 8px rgba(91, 88, 232, 0.08);
}

.source-upload-input {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.chat-input-actions {
  display: flex;
  flex-direction: column;
  align-self: stretch;
  justify-content: flex-end;
  gap: 6px;
}

.upload-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  min-height: 32px;
  padding: 6px 10px;
  border: 1px solid var(--color-border-light);
  border-radius: 6px;
  background: #fff;
  color: var(--color-text-secondary);
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;
}

.upload-btn:hover:not(:disabled) {
  border-color: var(--brand-400);
  color: var(--brand-600);
}

.upload-btn:disabled { opacity: 0.5; cursor: not-allowed; }

.send-btn {
  align-self: flex-end;
  min-height: 40px;
  padding: 8px 24px;
  border: none;
  border-radius: 10px;
  /* 品牌紫渐变 + 投影,替代纯平色 */
  background: linear-gradient(135deg, var(--brand-500), var(--brand-600));
  color: #fff;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  box-shadow: 0 3px 10px rgba(91, 88, 232, 0.28);
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.send-btn:hover:not(:disabled) {
  background: linear-gradient(135deg, var(--brand-400), var(--brand-600));
  box-shadow: 0 6px 16px rgba(91, 88, 232, 0.38);
  transform: translateY(-1px);
}
.send-btn:active:not(:disabled) { transform: translateY(0) scale(0.98); }
.send-btn:disabled { opacity: 0.5; cursor: not-allowed; box-shadow: none; }

/* 停止响应:红色醒目,运行中替换发送按钮 */
.stop-btn {
  align-self: flex-end;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 40px;
  padding: 8px 18px;
  border: 1px solid var(--color-danger);
  border-radius: 8px;
  background: var(--color-danger);
  color: #fff;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.stop-btn:hover { background: var(--sev-severe); border-color: var(--sev-severe); }

.stop-btn-icon {
  width: 9px;
  height: 9px;
  border-radius: 2px;
  background: #fff;
}

.drawer-enter-active,
.drawer-leave-active { transition: all 0.3s ease; }

.drawer-enter-active .chat-drawer,
.drawer-leave-active .chat-drawer { transition: transform 0.3s ease; }

.drawer-enter-from,
.drawer-leave-to { opacity: 0; }

.drawer-enter-from .chat-drawer,
.drawer-leave-to .chat-drawer { transform: translateY(8px) scale(.98); }

@media (max-width: 520px) {
  .chat-overlay { right: 16px; bottom: 16px; }
  .chat-fab { right: 16px; bottom: 16px; }
  .chat-drawer { width: calc(100vw - 32px); height: min(600px, calc(100dvh - 32px)); }
  /* 触控适配:关键按钮加大到 40px,更好点 */
  .close-btn { width: 40px; height: 40px; }
  .send-btn,
  .stop-btn { min-height: 40px; padding: 8px 22px; }
  .upload-btn { min-height: 40px; }
}
</style>
