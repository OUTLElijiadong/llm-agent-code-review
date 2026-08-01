<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  ChatDotRound,
  Close,
  DocumentCopy,
  Promotion,
  WarningFilled,
} from '@element-plus/icons-vue'

import {
  type AdminCopilotMessage,
} from '@/api/adminCopilot'
import { getAgentResponseSession, type AgentResponseSession } from '@/api/agentResponses'
import ResponseApprovalCard from '@/components/ai/responses/ResponseApprovalCard.vue'
import ResponseInputCard from '@/components/ai/responses/ResponseInputCard.vue'
import ResponseToolTimeline from '@/components/ai/responses/ResponseToolTimeline.vue'
import { renderMarkdown } from '@/utils/markdown'
import { streamResponses } from '@/utils/responsesStream'
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
  ResponseSensitiveResultEvent,
  ResponsesStreamHandle,
  ResponseStreamEvent,
} from '@/types/responses'
import {
  AGENT_RESPONSE_SESSION_POLL_INTERVAL_MS,
  isAgentResponseSessionActive,
  isAgentResponseSessionOccupied,
} from '@/utils/agentResponseSession'
import { ElMessage } from 'element-plus/es/components/message/index'

interface ChatEntry {
  id: string
  role: 'user' | 'assistant'
  time: string
  runId?: string
  payload: AdminCopilotMessage
  approval?: ResponseApprovalRequiredEvent & {
    status: 'pending' | 'submitting' | 'approved' | 'rejected'
  }
  inputRequest?: ResponseInputRequiredEvent & {
    answer: string
    answerSent?: boolean
    status: 'pending' | 'submitting' | 'answered'
  }
  toolCalls?: ResponseToolCall[]
  sensitiveResult?: ResponseSensitiveResultEvent
}

const ASSISTANT_NAME = 'Prism 管理副驾驶'
const SESSION_KEY = 'prism-admin-copilot-session'

const visible = ref(false)
const loading = ref(false)
const showTyping = ref(false)
const inputText = ref('')
const unreadAlerts = ref(0)
const messageArea = ref<HTMLElement | null>(null)
const expandedTables = ref<Set<string>>(new Set())

const messages = ref<ChatEntry[]>([])
const sessionRun = ref<AgentResponseSession['run']>(null)
const sessionRestoring = ref(true)

const sessionId = getOrCreateSessionId()
let activeResponse: ResponsesStreamHandle | null = null
let sessionRestoreStarted = false
let sessionPollTimer: number | undefined
let sessionPollStopped = false
let sessionPollGeneration = 0
let sessionSnapshotSignature = ''
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
  const id = `admin-${crypto.randomUUID()}`
  window.localStorage.setItem(SESSION_KEY, id)
  return id
}

function now(): string {
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date())
}

function assistantEntry(payload: AdminCopilotMessage): ChatEntry {
  return { id: crypto.randomUUID(), role: 'assistant', time: now(), payload }
}

function userEntry(content: string): ChatEntry {
  return {
    id: crypto.randomUUID(),
    role: 'user',
    time: now(),
    payload: { type: 'text', content },
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

function pendingEntry(session: AgentResponseSession, restoredTime: string): ChatEntry | null {
  const pending = session.pending
  if (!pending) return null
  const toolCalls: ResponseToolCall[] = []
  const target = assistantEntry({ type: 'text', content: '', status: 'completed' })
  target.time = restoredTime
  target.runId = session.run?.run_id
  target.toolCalls = toolCalls
  if (pending.type === 'response.approval.required') {
    attachApprovalToToolCall(toolCalls, pending.call_id, pending.tool_name, pending.arguments)
    target.approval = { ...pending, status: 'pending' }
  } else {
    attachInputToToolCall(toolCalls, pending)
    target.inputRequest = { ...pending, answer: '', status: 'pending' }
  }
  return target
}

function applySessionSnapshot(session: AgentResponseSession): void {
  sessionRun.value = session.run
  if (!loading.value) showTyping.value = isAgentResponseSessionActive(session.run?.status)
  if (isAgentResponseSessionActive(session.run?.status)) scheduleSessionPoll()
  else clearSessionPoll()

  const signature = JSON.stringify({
    run: session.run,
    messages: session.messages,
    pending: session.pending,
  })
  if (signature === sessionSnapshotSignature) return
  // Streaming owns the live DOM. Apply a poll snapshot after it settles so a
  // stale database read cannot erase text that is still arriving over SSE.
  if (loading.value) return
  sessionSnapshotSignature = signature
  const restoredTime = session.run?.updated_at
    ? new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(session.run.updated_at))
    : now()
  messages.value = session.messages.map((message) => ({
    id: crypto.randomUUID(),
    role: message.role,
    time: restoredTime,
    payload: {
      type: 'text',
      content: message.role === 'assistant'
        ? compactOutsideCodeBlocks(message.content)
        : message.content,
    },
  }))
  const pending = pendingEntry(session, restoredTime)
  if (pending) messages.value.push(pending)
}

async function pollSessionSnapshot(generation: number): Promise<void> {
  if (
    sessionPollStopped
    || generation !== sessionPollGeneration
    || !isAgentResponseSessionActive(sessionRun.value?.status)
  ) return
  try {
    const session = await getAgentResponseSession('admin', sessionId)
    if (sessionPollStopped || generation !== sessionPollGeneration) return
    applySessionSnapshot(session)
  } catch {
    // SSE remains the primary live channel; a transient poll failure is retried.
  } finally {
    if (
      !sessionPollStopped
      && generation === sessionPollGeneration
      && isAgentResponseSessionActive(sessionRun.value?.status)
    ) scheduleSessionPoll()
  }
}

async function restoreSession(): Promise<void> {
  if (sessionRestoreStarted) return
  sessionRestoreStarted = true
  try {
    const session = await getAgentResponseSession('admin', sessionId)
    applySessionSnapshot(session)
  } catch {
    // HTTP 层已给出错误提示；保留空对话仍允许用户重试。
  } finally {
    sessionRestoring.value = false
    await scrollToBottom()
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
    .map((entry) => ({ role: entry.role, content: entry.payload.content ?? '' }))
    .filter((entry) => entry.content.trim().length > 0)
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

async function scrollToBottom(): Promise<void> {
  await nextTick()
  if (messageArea.value) messageArea.value.scrollTop = messageArea.value.scrollHeight
}

function openPanel(): void {
  visible.value = true
  unreadAlerts.value = 0
  void scrollToBottom()
}

function closePanel(): void {
  visible.value = false
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
    const entry = messages.value[index]
    const calls = entry.toolCalls
    if (!calls?.some((call) => call.callId === callId)) continue
    applyResponseToolEvent(calls, event)
    return true
  }
  return false
}

function finishExistingTimelineToolCalls(runId: string | undefined, error: string): void {
  if (!runId) return
  for (const entry of messages.value) {
    if (entry.runId !== runId) continue
    finishResponseToolCalls(entry.toolCalls ?? [], 'failed', error)
  }
}

async function runResponse(payload: Record<string, unknown>): Promise<boolean> {
  invalidateSessionPoll()
  loading.value = true
  showTyping.value = true
  let rawText = ''
  let textTarget: ChatEntry | null = null
  let timelineTarget: ChatEntry | null = null
  const runToolCalls: ResponseToolCall[] = []
  let activeRunId = sessionRun.value?.run_id
  let protocolError = ''

  const syncTimeline = (): ChatEntry | null => {
    if (!runToolCalls.length) return null
    if (!timelineTarget) {
      timelineTarget = {
        ...assistantEntry({ type: 'text', content: '', status: 'completed' }),
        runId: activeRunId,
        toolCalls: [...runToolCalls],
      }
      messages.value.push(timelineTarget)
      if (!visible.value) unreadAlerts.value += 1
    } else {
      timelineTarget.toolCalls = [...runToolCalls]
    }
    return timelineTarget
  }

  const handle = streamResponses(payload, {
    onEvent(event) {
      if (event.type === 'response.created') {
        const runId = typeof event.response.id === 'string' ? event.response.id : sessionRun.value?.run_id ?? ''
        activeRunId = runId || activeRunId
        sessionRun.value = {
          run_id: runId,
          status: 'running',
          model: typeof event.response.model === 'string' ? event.response.model : sessionRun.value?.model ?? '',
          rounds: sessionRun.value?.rounds ?? 0,
          error: '',
          updated_at: new Date().toISOString(),
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
          messages.value.push(assistantEntry({ type: 'text', content, status: 'completed' }))
          textTarget = messages.value[messages.value.length - 1]
          if (!visible.value) unreadAlerts.value += 1
        } else {
          textTarget.payload.content = content
        }
      } else if (event.type === 'response.approval.required') {
        showTyping.value = false
        activeRunId = event.run_id
        sessionRun.value = { ...(sessionRun.value ?? { run_id: event.run_id, model: '', rounds: 0, error: '', updated_at: '' }), run_id: event.run_id, status: 'waiting_approval' }
        clearSessionPoll()
        attachApprovalToToolCall(runToolCalls, event.call_id, event.tool_name, event.arguments)
        const target = syncTimeline()
        const duplicate = messages.value.some((entry) => (
          entry.approval?.run_id === event.run_id && entry.approval.call_id === event.call_id
        ))
        if (!duplicate && target) {
          target.approval = { ...event, status: 'pending' }
        }
      } else if (event.type === 'response.input.required') {
        showTyping.value = false
        activeRunId = event.run_id
        sessionRun.value = { ...(sessionRun.value ?? { run_id: event.run_id, model: '', rounds: 0, error: '', updated_at: '' }), run_id: event.run_id, status: 'waiting_input' }
        clearSessionPoll()
        attachInputToToolCall(runToolCalls, event)
        const target = syncTimeline()
        const duplicate = messages.value.some((entry) => (
          entry.inputRequest?.run_id === event.run_id
          && entry.inputRequest.call_id === event.call_id
        ))
        if (!duplicate && target) {
          target.inputRequest = { ...event, answer: '', status: 'pending' }
        }
      } else if (event.type === 'response.sensitive.result') {
        showTyping.value = false
        messages.value.push({
          ...assistantEntry({ type: 'text', content: '', status: 'completed' }),
          sensitiveResult: event,
        })
        if (!visible.value) unreadAlerts.value += 1
      } else if (
        event.type === 'response.completed'
        || event.type === 'response.incomplete'
        || event.type === 'response.failed'
        || event.type === 'response.cancelled'
        || event.type === 'error'
      ) {
        showTyping.value = false
        protocolError ||= eventErrorMessage(event)
        const terminalStatus = event.type === 'response.completed'
          ? 'completed'
          : event.type === 'response.cancelled' ? 'cancelled' : 'failed'
        if (sessionRun.value) sessionRun.value = { ...sessionRun.value, status: terminalStatus, error: protocolError }
        invalidateSessionPoll()
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
      void scrollToBottom()
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
    await scrollToBottom()
  }
}

async function sendMessage(): Promise<void> {
  const content = inputText.value.trim()
  if (!content || loading.value || sessionRestoring.value || sessionBusy.value) return
  messages.value.push(userEntry(content))
  inputText.value = ''
  await scrollToBottom()
  await runResponse({
    action: 'start',
    surface: 'admin',
    session_id: sessionId,
    messages: conversationHistory(),
  })
}

async function decideApproval(entry: ChatEntry, decision: ResponseApprovalDecision): Promise<void> {
  const approval = entry.approval
  if (!approval || approval.status !== 'pending' || loading.value) return
  const { action, confirmation = '' } = decision
  approval.status = 'submitting'
  setTimelineCallStatus(approval.call_id, action === 'approve' ? 'running' : 'rejected')
  const succeeded = await runResponse({
    action,
    surface: 'admin',
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

async function copySensitiveValues(entry: ChatEntry): Promise<void> {
  const values = entry.sensitiveResult?.values ?? []
  if (!values.length) return
  try {
    await navigator.clipboard.writeText(values.join('\n'))
    ElMessage.success('已复制')
  } catch {
    ElMessage.error('复制失败，请手动选择文本')
  }
}

async function submitInput(entry: ChatEntry, selectedAnswer?: string): Promise<void> {
  const request = entry.inputRequest
  if (request && selectedAnswer !== undefined) request.answer = selectedAnswer
  const answer = request?.answer.trim() ?? ''
  if (!request || request.status !== 'pending' || !answer || loading.value) return
  request.status = 'submitting'
  if (!request.answerSent) {
    messages.value.push(userEntry(answer))
    request.answerSent = true
  }
  setTimelineCallStatus(request.call_id, 'running')
  const succeeded = await runResponse({
    action: 'answer',
    surface: 'admin',
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

function toggleTable(id: string): void {
  const next = new Set(expandedTables.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expandedTables.value = next
}

function visibleRows(entry: ChatEntry): Array<Record<string, unknown>> {
  const rows = entry.payload.rows ?? []
  return expandedTables.value.has(entry.id) ? rows : rows.slice(0, 10)
}

function handleKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape' && visible.value) closePanel()
}

function handleSubmitKey(event: KeyboardEvent): void {
  if (event.isComposing || event.keyCode === 229) return
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    void sendMessage()
  }
}

function handleAlertAction(prompt?: string): void {
  if (!prompt) return
  inputText.value = prompt
  void sendMessage()
}

onMounted(() => {
  window.addEventListener('keydown', handleKeydown)
  void restoreSession()
})
onBeforeUnmount(() => {
  sessionPollStopped = true
  invalidateSessionPoll()
  activeResponse?.abort()
  window.removeEventListener('keydown', handleKeydown)
})
</script>

<template>
  <div class="admin-copilot" :class="{ 'is-open': visible }">
    <button
      v-if="!visible"
      class="copilot-trigger"
      type="button"
      aria-label="打开管理副驾驶"
      title="管理副驾驶"
      @click="openPanel"
    >
      <el-icon><ChatDotRound /></el-icon>
      <span v-if="unreadAlerts" class="unread-dot" aria-label="有未读异常"></span>
    </button>

    <section v-else class="copilot-panel" role="dialog" aria-label="管理副驾驶对话">
      <header class="copilot-header">
        <div class="copilot-identity">
          <div class="copilot-avatar"><ChatDotRound /></div>
          <div>
            <strong>{{ ASSISTANT_NAME }}</strong>
            <span><i></i>在线</span>
          </div>
        </div>
        <button class="icon-button" type="button" aria-label="收起管理副驾驶" title="收起" @click="closePanel">
          <el-icon><Close /></el-icon>
        </button>
      </header>

      <div ref="messageArea" class="copilot-messages" aria-live="polite">
        <article
          v-for="entry in messages"
          :key="entry.id"
          class="message-row"
          :class="`is-${entry.role}`"
        >
          <div v-if="entry.role === 'assistant'" class="message-avatar"><ChatDotRound /></div>
          <div class="message-stack" :class="{ 'has-response-control': entry.toolCalls?.length || entry.approval || entry.inputRequest }">
            <div
              v-if="entry.payload.type === 'text' && entry.payload.content && entry.role === 'assistant'"
              class="message-bubble markdown-body"
              v-html="renderMarkdown(entry.payload.content)"
            />
            <div
              v-else-if="entry.payload.type === 'text' && entry.payload.content"
              class="message-bubble"
            >
              {{ entry.payload.content }}
            </div>

            <ResponseToolTimeline v-if="entry.toolCalls?.length" :calls="entry.toolCalls" />

            <section v-if="entry.sensitiveResult" class="sensitive-result" aria-live="assertive">
              <header>
                <strong>{{ entry.sensitiveResult.title }}</strong>
                <button
                  type="button"
                  class="sensitive-copy"
                  aria-label="复制全部一次性结果"
                  title="复制全部"
                  @click="copySensitiveValues(entry)"
                >
                  <el-icon><DocumentCopy /></el-icon>
                </button>
              </header>
              <code v-for="value in entry.sensitiveResult.values" :key="value">{{ value }}</code>
              <p>{{ entry.sensitiveResult.notice }}</p>
            </section>

            <div v-else-if="entry.payload.type === 'report'" class="report-card">
              <div class="card-title">{{ entry.payload.title }}</div>
              <p class="report-summary">{{ entry.payload.summary }}</p>
              <div class="report-counts">
                <div v-for="key in ['completed', 'in_progress', 'not_started']" :key="key">
                  <strong>{{ entry.payload.counts?.[key] ?? 0 }}</strong>
                  <span>{{ entry.payload.count_labels?.[key] ?? key }}</span>
                </div>
              </div>
              <div v-if="entry.payload.risks?.length" class="report-section">
                <b>风险与阻塞</b>
                <ul><li v-for="risk in entry.payload.risks" :key="risk">{{ risk }}</li></ul>
              </div>
              <div class="report-section">
                <b>下一步建议</b>
                <ol><li v-for="item in entry.payload.suggestions" :key="item">{{ item }}</li></ol>
              </div>
            </div>

            <div v-else-if="entry.payload.type === 'alert'" class="alert-card">
              <div class="card-title"><el-icon><WarningFilled /></el-icon>{{ entry.payload.title }}</div>
              <p>{{ entry.payload.description }}</p>
              <p><b>影响：</b>{{ entry.payload.impact }}</p>
              <p><b>建议：</b>{{ entry.payload.suggestion }}</p>
              <button class="warning-action" type="button" @click="handleAlertAction(entry.payload.action_prompt)">
                {{ entry.payload.action_label || '立即处理' }}
              </button>
            </div>

            <div v-else-if="entry.payload.type === 'table'" class="table-card">
              <div class="table-heading">
                <strong>{{ entry.payload.title }}</strong>
                <span>共 {{ entry.payload.total ?? 0 }} 条</span>
              </div>
              <div v-if="entry.payload.rows?.length" class="table-scroll">
                <table>
                  <thead><tr><th v-for="column in entry.payload.columns" :key="column">{{ column }}</th></tr></thead>
                  <tbody>
                    <tr v-for="(row, index) in visibleRows(entry)" :key="index">
                      <td v-for="column in entry.payload.columns" :key="column">{{ row[column] ?? '-' }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div v-else class="empty-table">没有查到数据</div>
              <button
                v-if="(entry.payload.rows?.length ?? 0) > 10"
                class="expand-button"
                type="button"
                @click="toggleTable(entry.id)"
              >
                {{ expandedTables.has(entry.id) ? '收起' : '展开全部' }}
              </button>
            </div>

            <ResponseApprovalCard
              v-if="entry.approval"
              :approval="entry.approval"
              :loading="loading"
              @decide="decideApproval(entry, $event)"
            />

            <ResponseInputCard
              v-if="entry.inputRequest"
              :request="entry.inputRequest"
              :loading="loading"
              @update:answer="entry.inputRequest.answer = $event"
              @submit="submitInput(entry, $event)"
            />

            <time>{{ entry.time }}</time>
          </div>
        </article>

        <div v-if="showTyping" class="typing-row">
          <div class="message-avatar"><ChatDotRound /></div>
          <div class="typing-bubble"><i></i><i></i><i></i></div>
        </div>
      </div>

      <footer class="copilot-input-area">
        <div class="composer">
          <textarea
            v-model="inputText"
            rows="1"
            maxlength="2000"
            :placeholder="sessionRestoring ? '正在恢复 Agent 会话' : sessionBusy ? '请先处理当前 Agent 任务' : '输入管理指令'"
            aria-label="输入管理指令"
            :disabled="loading || sessionRestoring || sessionBusy"
            @keydown="handleSubmitKey"
          ></textarea>
          <button type="button" class="send-button" :disabled="!canSend" aria-label="发送" title="发送" @click="sendMessage()">
            <el-icon><Promotion /></el-icon>
          </button>
        </div>
      </footer>
    </section>
  </div>
</template>

<style scoped lang="scss">
.admin-copilot {
  --agent-primary: #006eff;
  --agent-bg: #ffffff;
  --agent-text: #1f2329;
  --agent-text-secondary: #8f959e;
  --agent-border: #e5e6eb;
  --agent-danger: #d54941;
  --agent-warning: #ed7b2f;
  --agent-radius: 12px;
  --agent-shadow: 0 6px 24px rgba(0, 0, 0, 0.12);
  position: fixed;
  right: 24px;
  bottom: 24px;
  z-index: 1900;
  color: var(--agent-text);
}

button,
textarea,
input { font: inherit; }

.copilot-trigger {
  width: 56px;
  height: 56px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 50%;
  color: #fff;
  background: var(--agent-primary);
  box-shadow: 0 8px 24px rgba(0, 110, 255, 0.3);
  cursor: pointer;
  transition: transform 160ms ease, box-shadow 160ms ease;
}

.copilot-trigger:hover { transform: scale(1.05); box-shadow: 0 10px 28px rgba(0, 110, 255, 0.38); }
.copilot-trigger .el-icon { font-size: 26px; }
.unread-dot { position: absolute; top: 1px; right: 1px; width: 11px; height: 11px; border: 2px solid #fff; border-radius: 50%; background: var(--agent-danger); }

.copilot-panel {
  width: 380px;
  height: 600px;
  max-width: calc(100vw - 32px);
  max-height: calc(100dvh - 32px);
  display: grid;
  grid-template-rows: 64px minmax(0, 1fr) auto;
  overflow: hidden;
  border: 1px solid var(--agent-border);
  border-radius: var(--agent-radius);
  background: var(--agent-bg);
  box-shadow: var(--agent-shadow);
}

.copilot-header { display: flex; align-items: center; justify-content: space-between; padding: 0 14px 0 16px; border-bottom: 1px solid var(--agent-border); }
.copilot-identity { display: flex; align-items: center; gap: 10px; min-width: 0; }
.copilot-identity strong { display: block; font-size: 15px; line-height: 20px; }
.copilot-identity span { display: flex; align-items: center; gap: 5px; margin-top: 2px; color: var(--agent-text-secondary); font-size: 12px; }
.copilot-identity span i { width: 7px; height: 7px; border-radius: 50%; background: #2ba471; }
.copilot-avatar,
.message-avatar { display: grid; place-items: center; flex: 0 0 auto; border-radius: 50%; color: #fff; background: var(--agent-primary); }
.copilot-avatar { width: 36px; height: 36px; }
.message-avatar { width: 26px; height: 26px; margin-top: 3px; }
.copilot-avatar svg,
.message-avatar svg { width: 18px; height: 18px; }
.icon-button { width: 34px; height: 34px; display: grid; place-items: center; border: 0; border-radius: 50%; color: var(--agent-text-secondary); background: transparent; cursor: pointer; }
.icon-button:hover { color: var(--agent-text); background: #f2f3f5; }

.copilot-messages { min-height: 0; overflow-y: auto; padding: 16px 14px; background: #f7f8fa; }
.message-row { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 14px; }
.message-row.is-user { justify-content: flex-end; }
.message-stack { max-width: calc(100% - 34px); min-width: 0; }
.message-stack.has-response-control { width: calc(100% - 34px); }
.message-row.is-user .message-stack { display: flex; align-items: flex-end; flex-direction: column; }
.message-stack time { display: block; margin-top: 5px; color: var(--agent-text-secondary); font-size: 10px; }
.message-bubble { padding: 9px 11px; border-radius: 8px; color: var(--agent-text); background: #eef0f3; font-size: 13px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
.is-user .message-bubble { color: #fff; background: var(--agent-primary); }
.message-bubble.markdown-body {
  max-width: 100%;
  overflow-x: auto;
  white-space: normal;
  -webkit-overflow-scrolling: touch;
}
.message-bubble.markdown-body :deep(p) { margin: 0; }
.message-bubble.markdown-body :deep(table) {
  width: auto;
  min-width: 100%;
  border-collapse: collapse;
  white-space: nowrap;
}
.message-bubble.markdown-body :deep(th),
.message-bubble.markdown-body :deep(td) {
  padding: 7px 9px;
  border-bottom: 1px solid #dfe2e6;
  overflow-wrap: normal;
  text-align: left;
  white-space: nowrap;
  word-break: normal;
}
.message-bubble.markdown-body :deep(th) {
  color: #5b616b;
  background: #fafafa;
  font-weight: 600;
}
.message-bubble.markdown-body :deep(pre) {
  max-width: 100%;
  margin: 7px 0 0;
  padding: 9px;
  overflow-x: auto;
  border-radius: 6px;
  color: #e9edf4;
  background: #20242c;
  white-space: pre;
}

.action-card,
.report-card,
.alert-card,
.table-card { width: 292px; max-width: 100%; overflow: hidden; border: 1px solid var(--agent-border); border-radius: 8px; background: #fff; font-size: 12px; line-height: 1.5; }
.action-card,
.report-card,
.alert-card { padding: 12px; }
.action-card.is-danger { border-color: rgba(213, 73, 65, 0.75); }
.card-title { display: flex; align-items: center; gap: 5px; margin-bottom: 8px; font-size: 13px; font-weight: 650; }
.is-danger .card-title { color: var(--agent-danger); }
.action-card p,
.alert-card p { margin: 6px 0 0; color: #5b616b; }
.action-card .consequence { color: var(--agent-danger); }
.danger-input { width: 100%; height: 34px; margin-top: 10px; padding: 0 9px; border: 1px solid #d8dade; border-radius: 6px; outline: none; }
.danger-input:focus { border-color: var(--agent-danger); }
.card-actions { display: flex; gap: 8px; margin-top: 10px; }
.primary-action,
.secondary-action,
.warning-action { min-height: 32px; padding: 0 11px; border-radius: 6px; cursor: pointer; }
.primary-action { border: 1px solid var(--agent-primary); color: #fff; background: var(--agent-primary); }
.secondary-action { border: 1px solid var(--agent-border); color: #4e5969; background: #fff; }
.action-card.is-danger .primary-action { border-color: var(--agent-danger); background: var(--agent-danger); }
button:disabled { opacity: 0.45; cursor: not-allowed; }
.card-result { margin-top: 10px; padding: 6px 8px; border-radius: 6px; color: #137c4b; background: #e8f7ef; }
.card-result.cancelled { color: #6b7078; background: #f2f3f5; }
.response-tool-name { display: block; margin-top: 7px; overflow-wrap: anywhere; color: var(--agent-primary); }
.response-question { display: block; margin-bottom: 8px; font-weight: 650; }
.response-answer {
  width: 100%;
  min-height: 70px;
  resize: vertical;
  padding: 8px 9px;
  border: 1px solid #d8dade;
  border-radius: 6px;
  outline: none;
}
.response-answer:focus { border-color: var(--agent-primary); }
.response-answer-submit { margin-top: 9px; }

.sensitive-result { width: 100%; margin-top: 8px; padding: 10px 11px; border: 1px solid #d88b22; border-radius: 7px; background: #fff8e8; }
.sensitive-result header { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 7px; color: #8a4b08; }
.sensitive-result code { display: block; margin-top: 5px; padding: 6px 7px; overflow-wrap: anywhere; border: 1px solid #ead6ad; border-radius: 5px; background: #fff; color: #442604; font-size: 11px; user-select: all; }
.sensitive-result p { margin: 7px 0 0; color: #71552f; font-size: 10px; }
.sensitive-copy { display: grid; flex: 0 0 30px; width: 30px; height: 30px; place-items: center; border: 1px solid #d9b36d; border-radius: 5px; color: #8a4b08; background: #fff; cursor: pointer; }

.report-summary { margin: 0 0 10px; font-weight: 600; }
.report-counts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border: 1px solid #edf0f2; border-radius: 6px; }
.report-counts div { min-width: 0; padding: 8px 4px; text-align: center; }
.report-counts div + div { border-left: 1px solid #edf0f2; }
.report-counts strong,
.report-counts span { display: block; }
.report-counts strong { font-size: 17px; color: var(--agent-primary); }
.report-counts span { margin-top: 2px; color: var(--agent-text-secondary); font-size: 10px; overflow-wrap: anywhere; }
.report-section { margin-top: 10px; }
.report-section ul,
.report-section ol { margin: 5px 0 0; padding-left: 18px; color: #5b616b; }

.alert-card { border-left: 4px solid var(--agent-warning); }
.alert-card .card-title { color: #b55316; }
.warning-action { margin-top: 9px; border: 1px solid var(--agent-warning); color: #a44710; background: #fff7e8; }

.table-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 10px 11px; border-bottom: 1px solid var(--agent-border); }
.table-heading span { color: var(--agent-text-secondary); font-size: 10px; }
.table-scroll { overflow-x: auto; }
.table-scroll table { width: 100%; border-collapse: collapse; white-space: nowrap; }
.table-scroll th,
.table-scroll td { max-width: 150px; padding: 7px 9px; border-bottom: 1px solid #f0f1f2; text-align: left; overflow: hidden; text-overflow: ellipsis; }
.table-scroll th { color: #5b616b; background: #fafafa; font-weight: 600; }
.empty-table { padding: 28px 12px; color: var(--agent-text-secondary); text-align: center; }
.expand-button { width: 100%; padding: 7px; border: 0; color: var(--agent-primary); background: #fff; cursor: pointer; }

.typing-row { display: flex; align-items: flex-start; gap: 8px; }
.typing-bubble { display: flex; gap: 4px; padding: 11px 13px; border-radius: 8px; background: #eef0f3; }
.typing-bubble i { width: 5px; height: 5px; border-radius: 50%; background: #8f959e; animation: typing 1s infinite ease-in-out; }
.typing-bubble i:nth-child(2) { animation-delay: 120ms; }
.typing-bubble i:nth-child(3) { animation-delay: 240ms; }
@keyframes typing { 0%, 60%, 100% { transform: translateY(0); } 30% { transform: translateY(-4px); } }

.copilot-input-area { border-top: 1px solid var(--agent-border); background: #fff; }
.composer { display: grid; grid-template-columns: minmax(0, 1fr) 38px; align-items: end; gap: 8px; padding: 9px 10px 10px; }
.composer textarea { min-height: 38px; max-height: 84px; resize: none; padding: 9px 10px; border: 1px solid #d8dade; border-radius: 7px; color: var(--agent-text); outline: none; line-height: 18px; }
.composer textarea:focus { border-color: var(--agent-primary); box-shadow: 0 0 0 2px rgba(0, 110, 255, 0.1); }
.send-button { width: 38px; height: 38px; display: grid; place-items: center; border: 0; border-radius: 50%; color: #fff; background: var(--agent-primary); cursor: pointer; }

@media (max-width: 520px) {
  .admin-copilot { right: 12px; bottom: 12px; left: 12px; }
  .copilot-trigger { margin-left: auto; }
  .copilot-panel { width: 100%; height: min(600px, calc(100dvh - 24px)); max-width: none; max-height: none; }
}
</style>
