<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { CircleCheck, Close, Promotion, WarningFilled } from '@element-plus/icons-vue'

import { renderMarkdown } from '@/utils/markdown'
import dayjs from 'dayjs'
import { post } from '@/api/http'
import { getAgentResponseSession } from '@/api/agentResponses'
import { getProjects } from '@/api/project'
import { getReviewTasks } from '@/api/review'
import AgentAvatar from '@/components/agent/AgentAvatar.vue'
import ResponseApprovalCard from '@/components/ai/responses/ResponseApprovalCard.vue'
import ResponseInputCard from '@/components/ai/responses/ResponseInputCard.vue'
import ResponseToolTimeline from '@/components/ai/responses/ResponseToolTimeline.vue'
import type { AgentEvent, ClarifyPayload, ClarifyQuestion } from '@/types/agentEvent'
import type { AgentStatus } from '@/types/agent'
import { ElMessage } from 'element-plus/es/components/message/index'
import {
  CUSTOM_PROJECT_OPTION_VALUE,
  prepareClarifyAnswers,
  resolveProjectClarifyOptions,
  type ClarifyProjectOption,
} from '@/utils/clarifyProjectOptions'
import { streamResponses } from '@/utils/responsesStream'
import {
  AGENT_RESPONSE_SESSION_POLL_INTERVAL_MS,
  isAgentResponseSessionActive,
  isAgentResponseSessionOccupied,
} from '@/utils/agentResponseSession'
import {
  applyResponseToolEvent,
  attachApprovalToToolCall,
  attachInputToToolCall,
  finishResponseToolCalls,
  isResponseToolEvent,
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
  role: 'user' | 'assistant'
  content: string
  time: string
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
}

const props = defineProps<{ visible: boolean; prefill?: string }>()
const emit = defineEmits<{ 'update:visible': [value: boolean]; 'consumed-prefill': [] }>()

const messages = ref<ChatMessage[]>([])
const inputText = ref('')
const loading = ref(false)
const showTyping = ref(false)
const modelName = ref('deepseek-v4-flash')
const chatBody = ref<HTMLElement>()
const SESSION_KEY = 'prism-user-agent-session'
const sessionId = getOrCreateSessionId()
let activeResponse: ResponsesStreamHandle | null = null
let sessionRestoreStarted = false
let sessionPollTimer: number | undefined
let sessionPollStopped = false
let sessionPollGeneration = 0
let sessionSnapshotSignature = ''
const sessionRun = ref<Awaited<ReturnType<typeof getAgentResponseSession>>['run']>(null)
const sessionRestoring = ref(true)
const sessionBusy = computed(() => isAgentResponseSessionOccupied(sessionRun.value?.status))
const canSend = computed(() => (
  inputText.value.trim().length > 0
  && !loading.value
  && !sessionRestoring.value
  && !sessionBusy.value
))

function getOrCreateSessionId(): string {
  const current = window.localStorage.getItem(SESSION_KEY)
  if (current) return current
  const id = `user-${crypto.randomUUID()}`
  window.localStorage.setItem(SESSION_KEY, id)
  return id
}

function messageId(): string {
  return crypto.randomUUID()
}

async function restoreSession(): Promise<void> {
  if (sessionRestoreStarted) return
  sessionRestoreStarted = true
  try {
    const session = await getAgentResponseSession('user', sessionId)
    sessionRun.value = session.run
    if (session.run?.model) modelName.value = session.run.model
    const restoredTime = session.run?.updated_at
      ? dayjs(session.run.updated_at).format('HH:mm')
      : dayjs().format('HH:mm')
    messages.value = session.messages.map((message) => ({
      id: messageId(),
      role: message.role,
      content: message.role === 'assistant'
        ? compactOutsideCodeBlocks(message.content)
        : message.content,
      time: restoredTime,
    }))
    sessionSnapshotSignature = JSON.stringify({
      run: session.run,
      messages: session.messages,
      pending: session.pending,
    })
    const pending = session.pending
    if (!pending) {
      showTyping.value = isAgentResponseSessionActive(session.run?.status)
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
    clearSessionPoll()
  } catch {
    // HTTP 层已给出错误提示；保留本地会话不覆盖用户输入。
  } finally {
    sessionRestoring.value = false
    await nextTick()
    scrollToBottom()
  }
}

function clearSessionPoll(): void {
  if (sessionPollTimer !== undefined) {
    window.clearTimeout(sessionPollTimer)
    sessionPollTimer = undefined
  }
}

function invalidateSessionPoll(): void {
  sessionPollGeneration += 1
  clearSessionPoll()
}

function scheduleSessionPoll(): void {
  clearSessionPoll()
  if (sessionPollStopped || !isAgentResponseSessionActive(sessionRun.value?.status)) return
  const generation = sessionPollGeneration
  sessionPollTimer = window.setTimeout(() => {
    sessionPollTimer = undefined
    void pollSessionSnapshot(generation)
  }, AGENT_RESPONSE_SESSION_POLL_INTERVAL_MS)
}

async function pollSessionSnapshot(generation: number): Promise<void> {
  if (sessionPollStopped || generation !== sessionPollGeneration || !isAgentResponseSessionActive(sessionRun.value?.status)) return
  try {
    const session = await getAgentResponseSession('user', sessionId)
    if (sessionPollStopped || generation !== sessionPollGeneration) return
    sessionRun.value = session.run
    if (session.run?.model) modelName.value = session.run.model
    if (!loading.value) {
      const signature = JSON.stringify({ run: session.run, messages: session.messages, pending: session.pending })
      if (signature !== sessionSnapshotSignature) {
        sessionSnapshotSignature = signature
        const restoredTime = session.run?.updated_at ? dayjs(session.run.updated_at).format('HH:mm') : dayjs().format('HH:mm')
        messages.value = session.messages.map((message) => ({
          id: messageId(),
          role: message.role,
          content: message.role === 'assistant' ? compactOutsideCodeBlocks(message.content) : message.content,
          time: restoredTime,
        }))
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
    // SSE remains the primary channel; transient recovery errors are retried.
  } finally {
    if (
      !sessionPollStopped
      && generation === sessionPollGeneration
      && isAgentResponseSessionActive(sessionRun.value?.status)
    ) scheduleSessionPoll()
  }
}

/** 删除代码围栏之外的空白行，围栏内文本保持原样。 */
function compactOutsideCodeBlocks(value: string): string {
  const lines = value.replace(/\r\n?/g, '\n').split('\n')
  const output: string[] = []
  let fence: '```' | '~~~' | null = null
  for (const line of lines) {
    const marker = line.trimStart().startsWith('```')
      ? '```'
      : line.trimStart().startsWith('~~~') ? '~~~' : null
    if (marker) {
      if (!fence) fence = marker
      else if (fence === marker) fence = null
      output.push(line)
      continue
    }
    if (!fence && line.trim() === '') continue
    output.push(line)
  }
  return output.join('\n')
}

function conversationHistory(): Array<{ role: 'user' | 'assistant'; content: string }> {
  return messages.value
    .filter((message) => message.content.trim().length > 0)
    .map((message) => ({ role: message.role, content: message.content }))
}

function eventErrorMessage(event: ResponseStreamEvent): string {
  if (event.type === 'error') return event.error?.message || event.message || ''
  if (event.type === 'response.incomplete') return '模型响应未完整结束，请重新发起任务'
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
  return error instanceof Error && error.message ? error.message : 'Agent 请求失败'
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

async function runResponse(payload: Record<string, unknown>): Promise<boolean> {
  invalidateSessionPoll()
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
      } else if (isResponseToolEvent(event)) {
        showTyping.value = false
        if (!applyExistingTimelineToolEvent(event)) {
          applyResponseToolEvent(runToolCalls, event)
          syncTimeline()
        }
      } else if (event.type === 'response.output_text.delta') {
        rawText += event.delta
        const content = compactOutsideCodeBlocks(rawText)
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
        showTyping.value = false
        if (sessionRun.value) {
          sessionRun.value = {
            ...sessionRun.value,
            status: event.type === 'response.completed'
              ? 'completed'
              : event.type === 'response.cancelled' ? 'cancelled' : 'failed',
          }
        }
        invalidateSessionPoll()
        protocolError ||= eventErrorMessage(event)
        const failed = event.type !== 'response.completed'
        const terminalError = failed ? protocolError : '响应已结束，但工具未返回完成事件'
        finishResponseToolCalls(
          runToolCalls,
          'failed',
          terminalError,
        )
        finishExistingTimelineToolCalls(activeRunId, terminalError)
        syncTimeline()
      }
      void nextTick().then(scrollToBottom)
    },
  })
  activeResponse = handle

  try {
    await handle.done
    if (protocolError) {
      ElMessage.error(protocolError)
      return false
    }
    return true
  } catch (error) {
    if (!(error instanceof Error && error.name === 'AbortError')) {
      ElMessage.error(requestErrorMessage(error))
      if (isAgentResponseSessionActive(sessionRun.value?.status)) scheduleSessionPoll()
    }
    return false
  } finally {
    if (activeResponse === handle) activeResponse = null
    loading.value = false
    showTyping.value = false
    await nextTick()
    scrollToBottom()
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
}

const TYPE_LABELS: Record<AgentEvent['type'], string> = {
  dispatch: '派发',
  thinking: '思考',
  progress: '进行中',
  complete: '完成',
  failed: '失败',
  clarify: '等待用户',
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

  await nextTick()
  scrollToBottom()
  await runResponse({
    action: 'start',
    surface: 'user',
    session_id: sessionId,
    messages: conversationHistory(),
  })
}

async function decideApproval(
  message: ChatMessage,
  decision: ResponseApprovalDecision,
): Promise<void> {
  const approval = message.approval
  if (!approval || approval.status !== 'pending' || loading.value) return
  const { action, confirmation = '' } = decision
  approval.status = 'submitting'
  setTimelineCallStatus(approval.call_id, action === 'approve' ? 'running' : 'rejected')
  const succeeded = await runResponse({
    action,
    surface: 'user',
    session_id: sessionId,
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
    session_id: sessionId,
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
  }
}

/**
 * 计算双层调度调用链的总耗时
 * @param steps - 调用链步骤列表
 * @returns 总耗时(毫秒)
 */
function planTotalMs(steps: PlanStep[]): number {
  return steps.reduce((sum, s) => sum + (s.duration_ms || 0), 0)
}

function scrollToBottom(): void {
  if (chatBody.value) {
    chatBody.value.scrollTop = chatBody.value.scrollHeight
  }
}

function close(): void {
  emit('update:visible', false)
}

watch(() => props.visible, (val) => {
  if (val) {
    nextTick(() => scrollToBottom())
  }
})

watch(() => props.prefill, (prefill) => {
  if (!prefill) return
  inputText.value = prefill
  emit('consumed-prefill')
}, { immediate: true })

function handleEscape(event: KeyboardEvent): void {
  if (event.key === 'Escape' && props.visible) close()
}

onMounted(() => {
  window.addEventListener('keydown', handleEscape)
  void restoreSession()
})

onBeforeUnmount(() => {
  sessionPollStopped = true
  invalidateSessionPoll()
  activeResponse?.abort()
  window.clearTimeout(projectSearchTimer)
  window.removeEventListener('keydown', handleEscape)
})
</script>

<template>
  <Teleport to="body">
    <button v-if="!visible" class="chat-fab" type="button" aria-label="打开棱镜小助" title="棱镜小助" @click="emit('update:visible', true)">
      <el-icon><Promotion /></el-icon>
    </button>
    <Transition name="drawer">
      <div v-if="visible" class="chat-overlay">
        <div class="chat-drawer">
          <div class="chat-header">
            <div class="chat-title">
              <el-icon class="title-icon"><Promotion /></el-icon>
              <span>Agent 助手</span>
              <span class="model-tag font-mono">{{ modelName }}</span>
            </div>
            <button
              class="close-btn"
              type="button"
              aria-label="关闭 Agent 助手"
              title="关闭 Agent 助手"
              @click="close"
            >
              <el-icon><Close /></el-icon>
            </button>
          </div>

          <div ref="chatBody" class="chat-body">
            <div v-for="(msg, i) in messages" :key="msg.id ?? i" class="msg-row" :class="msg.role">
              <div class="msg-avatar">{{ msg.role === 'user' ? 'U' : 'AG' }}</div>
              <div class="msg-bubble" :class="{ 'has-response-control': msg.toolCalls?.length || msg.approval || msg.inputRequest }">
                <!-- 步骤气泡: 仅对 assistant + 有 steps 时展示 -->
                <details
                  v-if="msg.role === 'assistant' && msg.steps && msg.steps.length"
                  class="step-stream"
                  open
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
                    <span class="plan-icon">🌳</span>
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

                <div
                  v-if="msg.role === 'assistant' && msg.content"
                  class="msg-content markdown-body"
                  v-html="renderMarkdown(msg.content)"
                />
                <div v-else-if="msg.role === 'user'" class="msg-content">{{ msg.content }}</div>

                <ResponseToolTimeline v-if="msg.toolCalls?.length" :calls="msg.toolCalls" />

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

            <div v-if="showTyping" class="msg-row assistant">
              <div class="msg-avatar">AG</div>
              <div class="msg-bubble typing">
                <span class="typing-dot" />
                <span class="typing-dot" />
                <span class="typing-dot" />
              </div>
            </div>
          </div>

          <div class="chat-input-area">
            <textarea
              v-model="inputText"
              class="chat-input"
              :placeholder="sessionRestoring ? '正在恢复 Agent 会话' : sessionBusy ? '请先处理当前 Agent 任务' : '输入问题,Enter 发送,Shift+Enter 换行'"
              rows="2"
              :disabled="loading || sessionRestoring || sessionBusy"
              @keydown="handleKeydown"
            />
            <button
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
    </Transition>
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
  width: min(380px, calc(100vw - 32px));
  height: min(600px, calc(100dvh - 48px));
  background: #fff;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--color-border-light);
  border-radius: 12px;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.12);
  overflow: hidden;
}

.chat-fab {
  position: fixed; right: 24px; bottom: 24px; z-index: 3000;
  width: 56px; height: 56px; border: 0; border-radius: 50%;
  background: var(--brand-500); color: #fff; font-size: 22px; cursor: pointer;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.18); transition: transform .16s ease;
}
.chat-fab:hover { transform: scale(1.05); }

.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--color-border-light);
  flex-shrink: 0;
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

.model-tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 4px;
  background: var(--brand-50);
  color: var(--brand-600);
  border: 1px solid var(--brand-200);
}

.close-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
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

.chat-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

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
}

.msg-row.user .msg-avatar { background: var(--brand-500); color: #fff; }
.msg-row.assistant .msg-avatar {
  background: linear-gradient(135deg, var(--brand-500), var(--accent-400));
  color: #fff;
}

.msg-bubble {
  max-width: calc(100% - 48px);
  padding: 10px 14px;
  border-radius: 12px;
  font-size: 13.5px;
  line-height: 1.6;
}

.msg-bubble.has-response-control { width: calc(100% - 48px); }

.msg-row.user .msg-bubble {
  background: var(--brand-50);
  border: 1px solid var(--brand-100);
  border-top-right-radius: 4px;
}

.msg-row.assistant .msg-bubble {
  background: var(--gray-50);
  border: 1px solid var(--color-border-light);
  border-top-left-radius: 4px;
}

.msg-content {
  white-space: pre-wrap;
  word-break: break-word;
}

.msg-content.markdown-body :deep(p)        { margin: 0 0 8px; }
.msg-content.markdown-body :deep(p:last-child) { margin-bottom: 0; }
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
.msg-content.markdown-body :deep(ul),
.msg-content.markdown-body :deep(ol) { padding-left: 18px; margin: 4px 0; }
.msg-content.markdown-body :deep(li) { margin: 2px 0; }

.msg-time {
  font-size: 10px;
  color: var(--color-text-placeholder);
  margin-top: 4px;
  text-align: right;
}

.msg-bubble.typing {
  display: flex;
  gap: 4px;
  align-items: center;
  padding: 14px 18px;
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

.plan-icon { font-size: 14px; }

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
}

.chat-input {
  flex: 1;
  border: 1px solid var(--color-border-light);
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.5;
  resize: none;
  outline: none;
  font-family: inherit;
  transition: border-color var(--transition-fast);
}

.chat-input:focus { border-color: var(--brand-400); }

.send-btn {
  align-self: flex-end;
  padding: 8px 20px;
  border: none;
  border-radius: 8px;
  background: var(--brand-500);
  color: #fff;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.send-btn:hover:not(:disabled) { background: var(--brand-600); }
.send-btn:disabled { opacity: 0.5; cursor: not-allowed; }

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
}
</style>
