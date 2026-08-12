<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  Close,
  DocumentCopy,
  Promotion,
  WarningFilled,
} from '@element-plus/icons-vue'

import {
  type AdminCopilotMessage,
} from '@/api/adminCopilot'
import { getAgentResponseSession, type AgentResponseSession } from '@/api/agentResponses'
import type { AgentMeshMessage } from '@/api/agentMesh'
import { getAgentTeam, listAgentTeams, type AgentTeamDetail } from '@/api/agentTeams'
import { createProject, updateProject, deleteProject } from '@/api/project'
import { upload as uploadCodeFile } from '@/api/codeFile'
import AgentSessionSwitcher from '@/components/ai/AgentSessionSwitcher.vue'
import PrismMascot from '@/components/ai/PrismMascot.vue'
import ResponseApprovalCard from '@/components/ai/responses/ResponseApprovalCard.vue'
import ResponseInputCard from '@/components/ai/responses/ResponseInputCard.vue'
import ResponseToolTimeline from '@/components/ai/responses/ResponseToolTimeline.vue'
import AgentTeamTrace from '@/components/ai/AgentTeamTrace.vue'
import AgentNavLink from '@/components/ai/AgentNavLink.vue'
import { renderMarkdown } from '@/utils/markdown'
import { extractNavigateDirectives } from '@/utils/agentNavigation'
import type { AgentNavigateDirective } from '@/types/agentGuide'
import { streamResponses } from '@/utils/responsesStream'
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
  ResponseSensitiveResultEvent,
  ResponsesStreamHandle,
  ResponseStreamEvent,
} from '@/types/responses'
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
import {
  autoTitleAgentChatSession,
  loadAgentChatSnapshot,
  saveAgentChatSnapshot,
  type AgentChatSessionMeta,
} from '@/utils/agentChatSessions'
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
  /** 助手回复末尾解析出的"带我去"导航指令 */
  navigations?: AgentNavigateDirective[]
}

const ASSISTANT_NAME = '小菱 · 管理副驾驶'
const MASCOT_NAME = '小菱'
const WELCOME_TEXT = `你好,我是${MASCOT_NAME},Prism 的管理副驾驶!我可以帮你巡查系统态势、审批运维操作、生成平台报表。点击「+」可开新对话,多个任务并行处理。`
const LEGACY_SESSION_KEY = 'prism-admin-copilot-session'

const visible = ref(false)
const loading = ref(false)
const showTyping = ref(false)
const inputText = ref('')
const uploading = ref(false)
const uploadStatus = ref('')
const dragActive = ref(false)
const unreadAlerts = ref(0)
const messageArea = ref<HTMLElement | null>(null)
const { panelRef, style: panelStyle, dragging, restoreOrAnchor, beginDrag, moveDrag, endDrag } = useFloatingChatPosition('admin')
const expandedTables = ref<Set<string>>(new Set())

const messages = ref<ChatEntry[]>([])
const sessionRun = ref<AgentResponseSession['run']>(null)
const sessionRestoring = ref(true)
const sessionPollError = ref('')
const sessionLastPolledAt = ref('')
const agentTeams = ref<AgentTeamDetail[]>([])
const agentTeamLoading = ref(false)
const agentTeamError = ref('')
const router = useRouter()

const sessionId = ref('')
const switcherRef = ref<InstanceType<typeof AgentSessionSwitcher> | null>(null)
const meshSessions = ref<AgentChatSessionMeta[]>([])
const backgroundBusySessions = new Set<string>()
const lastActiveToolName = ref('')
let activeResponse: ResponsesStreamHandle | null = null
let sessionRestoreStarted = false
let sessionPollTimer: number | undefined
let sessionPollStopped = false
let sessionPollGeneration = 0
let sessionSnapshotSignature = ''
const sessionBusy = computed(() => isAgentResponseSessionOccupied(sessionRun.value?.status))

/** 失败/未完成/超轮数的运行可手动重试（回退策略入口） */
const canRetryRun = computed(() => {
  const status = sessionRun.value?.status
  return Boolean(status && ['failed', 'incomplete', 'max_rounds_exceeded'].includes(status) && !loading.value)
})

/** 吉祥物与标题栏共享的 agent 状态:运行中/等待用户/空闲 */
const mascotStatus = computed<'idle' | 'running' | 'waiting'>(() => {
  const status = sessionRun.value?.status
  if (loading.value || isAgentResponseSessionActive(status)) return 'running'
  if (isAgentResponseSessionWaiting(status)) return 'waiting'
  return 'idle'
})
const runStatusLabel = computed(() => (
  mascotStatus.value === 'running' ? '运行中' : mascotStatus.value === 'waiting' ? '等待你操作' : '空闲'
))
const canSend = computed(() => (
  inputText.value.trim().length > 0
  && !loading.value
  && !sessionRestoring.value
  && !sessionBusy.value
))

const meshBridge = createAgentMeshBridge({
  surface: 'admin',
  getSessionId: () => sessionId.value,
  getTitle: () => meshSessions.value.find((item) => item.id === sessionId.value)?.title ?? '管理端小菱对话',
  getSessions: () => meshSessions.value,
  getActiveRun: () => sessionRun.value,
  isBusy: isMeshSessionBusy,
  onMessage: handleMeshMessage,
})

function welcomeEntry(): ChatEntry {
  return assistantEntry({ type: 'text', content: WELCOME_TEXT, status: 'completed' })
}

/** 把当前会话的关键上下文写入本地快照,供切换会话与忙碌标记使用。 */
function persistSnapshot(): void {
  if (!sessionId.value) return
  saveAgentChatSnapshot(sessionId.value, {
    messages: messages.value.map((entry) => ({ role: entry.role, content: entry.payload.content ?? '' })),
    runStatus: sessionRun.value?.status ?? null,
    updatedAt: Date.now(),
  })
}

/** 会话切换:中止本地流视图与轮询,清空后展示欢迎语并恢复目标会话。 */
async function handleSessionSelect(nextSessionId: string): Promise<void> {
  // 旧会话若在流式/运行中,保持其快照忙碌标记;服务端运行不受影响,可稍后切回接管。
  const wasBusy = isAgentResponseSessionOccupied(sessionRun.value?.status)
  // 面板重建后内存是初始空状态,不能用它覆盖已有快照;
  // 仅当当前会话有真实内容(非欢迎语)或运行状态时才持久化。
  const hasRealContent = messages.value.some((entry) => {
    const content = entry.payload.content ?? ''
    return content.trim().length > 0 && content.trim() !== WELCOME_TEXT.trim()
  })
  if (hasRealContent || sessionRun.value?.status) persistSnapshot()
  if (wasBusy) switcherRef.value?.setBusy(sessionId.value, true)
  activeResponse?.abort()
  activeResponse = null
  sessionPollStopped = true
  invalidateSessionPoll()
  sessionPollStopped = false
  sessionSnapshotSignature = ''
  sessionRestoreStarted = false
  sessionId.value = nextSessionId
  sessionRun.value = null
  agentTeams.value = []
  agentTeamError.value = ''
  loading.value = false
  showTyping.value = false
  lastActiveToolName.value = ''
  sessionRestoring.value = true
  messages.value = [welcomeEntry()]
  await scrollToBottom()
  void restoreSession()
}

/** 切换器初始化后补同步一次忙碌状态,避免初始化竞态导致标记丢失。 */
function handleSwitcherReady(metas: AgentChatSessionMeta[]): void {
  meshSessions.value = metas
  syncBusy()
  void meshBridge.syncNow()
}

function syncBusy(): void {
  const busy = isAgentResponseSessionOccupied(sessionRun.value?.status)
  if (!busy) backgroundBusySessions.delete(sessionId.value)
  switcherRef.value?.setBusy(sessionId.value, busy)
}

function isMeshSessionBusy(targetSessionId: string): boolean {
  if (targetSessionId === sessionId.value) {
    return loading.value || sessionRestoring.value || sessionBusy.value
  }
  if (backgroundBusySessions.has(targetSessionId)) return true
  return isAgentResponseSessionOccupied(loadAgentChatSnapshot(targetSessionId)?.runStatus)
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

/** 团队账本是服务端事实源;会话恢复和每次轮询都重新读取当前会话的最新团队。 */
async function refreshAgentTeam(generation?: number): Promise<void> {
  if (!sessionId.value) return
  const requestedSessionId = sessionId.value
  const isCurrent = (): boolean => requestedSessionId === sessionId.value
    && (generation === undefined || generation === sessionPollGeneration)
  agentTeamLoading.value = true
  try {
    const listed = await listAgentTeams({ surface: 'admin', session_id: requestedSessionId, limit: 20 })
    if (!isCurrent()) return
    if (!listed.items.length) {
      agentTeams.value = []
      agentTeamError.value = ''
      return
    }
    const details = await Promise.all(listed.items.map((item) => getAgentTeam(item.team_id)))
    if (!isCurrent()) return
    agentTeams.value = details
    agentTeamError.value = ''
  } catch {
    if (isCurrent()) agentTeamError.value = '团队状态同步暂时中断'
  } finally {
    if (isCurrent()) agentTeamLoading.value = false
  }
}

function scheduleSessionPoll(): void {
  clearSessionPoll()
  if (sessionPollStopped || !sessionId.value) return
  const generation = sessionPollGeneration
  sessionPollTimer = window.setTimeout(() => {
    sessionPollTimer = undefined
    void pollSessionSnapshot(generation)
  }, agentResponseSessionPollInterval(sessionRun.value?.status))
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

function restoredEntries(session: AgentResponseSession, restoredTime: string): ChatEntry[] {
  // 早期版本曾把本地欢迎语带入模型上下文并被服务端持久化,恢复时去重
  const restored: ChatEntry[] = session.messages
    .filter((message) => message.content.trim() !== WELCOME_TEXT.trim())
    .map((message) => {
      if (message.role !== 'assistant') {
        return {
          id: crypto.randomUUID(),
          role: message.role,
          time: restoredTime,
          payload: { type: 'text' as const, content: message.content },
        }
      }
      const { cleaned, directives } = extractNavigateDirectives(message.content)
      return {
        id: crypto.randomUUID(),
        role: message.role,
        time: restoredTime,
        payload: { type: 'text' as const, content: compactOutsideCodeBlocks(cleaned) },
        navigations: directives.length ? directives : undefined,
      }
    })
  const toolCalls = [
    ...agentMeshToolCalls(session.mesh_messages, `session:admin:${session.session_id}`),
    ...responseToolCallsFromEvents(session.events),
  ]
  if (!toolCalls.length) return restored
  const timeline: ChatEntry = {
    ...assistantEntry({ type: 'text', content: '', status: 'completed' }),
    time: restoredTime,
    runId: session.run?.run_id,
    toolCalls,
  }
  let conclusionIndex = -1
  for (let index = restored.length - 1; index >= 0; index -= 1) {
    if (restored[index].role === 'assistant' && restored[index].payload.content?.trim()) {
      conclusionIndex = index
      break
    }
  }
  restored.splice(conclusionIndex >= 0 ? conclusionIndex : restored.length, 0, timeline)
  return restored
}

function applySessionSnapshot(session: AgentResponseSession): void {
  sessionRun.value = session.run
  if (!loading.value) showTyping.value = isAgentResponseSessionActive(session.run?.status)
  scheduleSessionPoll()

  const signature = JSON.stringify({
    run: session.run,
    messages: session.messages,
    events: session.events,
    mesh_messages: session.mesh_messages,
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
  const restored = restoredEntries(session, restoredTime)
  // 服务端恢复出历史时,按欢迎语+历史整体重建,避免与本地占位重复
  if (restored.length) messages.value = [welcomeEntry(), ...restored]
  const pending = pendingEntry(session, restoredTime)
  if (pending) messages.value.push(pending)
}

async function pollSessionSnapshot(generation: number): Promise<void> {
  if (
    sessionPollStopped
    || generation !== sessionPollGeneration
  ) return
  try {
    const session = await getAgentResponseSession('admin', sessionId.value)
    if (sessionPollStopped || generation !== sessionPollGeneration) return
    sessionPollError.value = ''
    sessionLastPolledAt.value = now()
    applySessionSnapshot(session)
    await refreshAgentTeam(generation)
  } catch {
    sessionPollError.value = '同步暂时中断,正在重试'
  } finally {
    syncBusy()
    persistSnapshot()
    if (!sessionPollStopped && generation === sessionPollGeneration) scheduleSessionPoll()
  }
}

async function restoreSession(): Promise<void> {
  if (sessionRestoreStarted) return
  sessionRestoreStarted = true
  try {
    const session = await getAgentResponseSession('admin', sessionId.value)
    applySessionSnapshot(session)
    await refreshAgentTeam()
  } catch {
    // HTTP 层已给出错误提示；保留空对话仍允许用户重试。
  } finally {
    sessionRestoring.value = false
    syncBusy()
    persistSnapshot()
    await scrollToBottom()
    void meshBridge.syncNow()
  }
}

async function handleMeshMessage(message: AgentMeshMessage, targetSessionId: string): Promise<boolean> {
  if (targetSessionId !== sessionId.value) {
    return runBackgroundMeshMessage(message, targetSessionId)
  }
  if (!findAgentMeshTimeline(messages.value, message.message_id)) {
    messages.value.push({
      ...assistantEntry({ type: 'text', content: '', status: 'completed' }),
      toolCalls: agentMeshToolCalls([message], `session:admin:${sessionId.value}`),
    })
  }
  await scrollToBottom()
  const succeeded = await runResponse({
    action: 'start',
    surface: 'admin',
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
  const handle = streamResponses({
    action: 'start',
    surface: 'admin',
    session_id: targetSessionId,
    messages: [],
    mesh_message_id: message.message_id,
  }, {
    onEvent(event) {
      if (event.type === 'response.approval.required' || event.type === 'response.input.required') {
        waiting = true
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
    return true
  } catch {
    return false
  } finally {
    if (!waiting) {
      backgroundBusySessions.delete(targetSessionId)
      switcherRef.value?.setBusy(targetSessionId, false)
    }
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
    .map((entry) => ({ role: entry.role, content: entry.payload.content ?? '' }))
    // 欢迎语是本地开屏气泡,不参与模型上下文,避免被服务端持久化后恢复重复
    .filter((entry) => entry.content.trim().length > 0 && entry.content.trim() !== WELCOME_TEXT.trim())
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

/**
 * 执行指令导航:仅跳站内路由,目标由 AgentNavLink 同源守卫再次校验。
 * 导航前收起面板,让目标管理页完整呈现,模拟管理员真实操作路径。
 */
function followNavigation(directive: AgentNavigateDirective): void {
  if (!directive.route.startsWith('/')) return
  // 仅站内跳转,不再收起面板——管理员可能还要参考对话内容继续操作。
  void router.push(directive.route)
}

/**
 * 拦截助手回复中的站内 markdown 链接点击:
 * 命中路由表则由 AgentNavLink 同源守卫决定是否渲染,点击后 SPA 内跳转。
 */
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

function onDragOver(event: DragEvent): void {
  if (event.dataTransfer?.types?.includes('Files')) dragActive.value = true
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'
}

function onDragLeave(event: DragEvent): void {
  if (!event.currentTarget || !(event.currentTarget as HTMLElement).contains(event.relatedTarget as Node | null)) {
    dragActive.value = false
  }
}

async function onDrop(event: DragEvent): Promise<void> {
  dragActive.value = false
  const files = Array.from(event.dataTransfer?.files ?? [])
  if (!files.length || uploading.value) return
  uploading.value = true
  uploadStatus.value = `准备上传 ${files.length} 个文件…`
  try {
    const images = files.filter((f) => IMAGE_EXTS.has(f.name.split('.').pop()?.toLowerCase() ?? ''))
    const codeFiles = files.filter((f) => CODE_EXTS.has(f.name.split('.').pop()?.toLowerCase() ?? ''))
    if (images.length && !codeFiles.length) {
      uploadStatus.value = ''
      ElMessage.info('图片会作为项目附件上传；若要让小菱帮你创建代码项目，请再拖入至少一个代码文件')
      return
    }
    const targets = files.slice(0, 20)
    const skipped = files.length - targets.length
    if (skipped > 0) ElMessage.warning(`单次最多上传 20 个文件,已跳过后面的 ${skipped} 个`)
    await uploadFilesAsProject(targets, images.length)
  } catch (err) {
    uploadStatus.value = ''
    ElMessage.error(`上传失败: ${err instanceof Error ? err.message : '请重试'}`)
  } finally {
    uploading.value = false
  }
}

/** 把拖拽的文件建成一个新项目并导入,然后让管理端小菱接手引导下一步。 */
async function uploadFilesAsProject(files: File[], imageCount = 0): Promise<void> {
  uploadStatus.value = `正在验证 ${files.length} 个文件…`
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
  }
  if (!readableFiles.length) {
    uploadStatus.value = ''
    throw new Error(`没有可上传的文件${preflightFailures.length ? ` (${preflightFailures[0]})` : ''}`)
  }
  const base = readableFiles.find((file) => !IMAGE_EXTS.has(file.name.split('.').pop()?.toLowerCase() ?? ''))?.name.replace(/\.[^.]+$/, '') || '拖拽上传'
  const suffix = `${new Date().toISOString().slice(5, 10).replace('-', '')}-${crypto.randomUUID().slice(0, 6)}`
  const projectName = `${base}-${suffix}`
  const language = inferProjectLanguage(readableFiles)
  uploadStatus.value = `正在创建项目「${projectName}」…`
  const created = await createProject({ project_name: projectName, description: `管理端小菱拖拽上传导入(${readableFiles.map((f) => f.name).join(', ')})`, language })
  const projectId = created.id
  let okCount = 0
  const failures: string[] = [...preflightFailures]
  for (let i = 0; i < readableFiles.length; i++) {
    const file = readableFiles[i]
    uploadStatus.value = `正在上传 ${i + 1}/${readableFiles.length}: ${file.name}`
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
  }
  uploadStatus.value = ''
  if (!okCount) {
    try { await deleteProject(projectId) } catch { /* 仍优先把真实上传失败反馈给用户 */ }
    throw new Error(`所有文件上传失败,未保留项目${failures.length ? ` (${failures[0]})` : ''}`)
  }
  const uploadedLanguage = inferProjectLanguage(readableFiles.filter((f) => !IMAGE_EXTS.has(f.name.split('.').pop()?.toLowerCase() ?? '')))
  if (uploadedLanguage !== language) {
    try { await updateProject(projectId, { language: uploadedLanguage }) } catch { /* 不影响已上传文件 */ }
  }
  const imageNote = imageCount ? `（含 ${imageCount} 张图片附件）` : ''
  const summary = `我已帮你把 ${okCount} 个文件${imageNote}上传到项目「${projectName}」(#${projectId},语言 ${uploadedLanguage})${failures.length ? `,${failures.length} 个失败(${failures[0]})` : ''}。接下来你想让我帮你对这个项目做什么?比如发起代码审查、安全扫描或沙箱部署。`
  messages.value.push(assistantEntry({ type: 'text', content: summary }))
  await scrollToBottom()
  if (failures.length) ElMessage.warning(`已上传 ${okCount} 个,${failures.length} 个失败`)
  else ElMessage.success(`已创建项目「${projectName}」并上传 ${okCount} 个文件`)
  await runResponse({
    action: 'start',
    surface: 'admin',
    session_id: sessionId.value,
    messages: [...conversationHistory(), { role: 'user', content: `我刚通过拖拽上传了 ${okCount} 个文件,已建好项目「${projectName}」(id=${projectId},语言 ${language})。请告诉我下一步可以做什么。` }],
  })
}

function onMessageClick(event: MouseEvent): void {
  const anchor = (event.target as HTMLElement | null)?.closest?.('a')
  if (!anchor) return
  const href = anchor.getAttribute('href') ?? ''
  if (!href.startsWith('/') || href.startsWith('//')) return
  event.preventDefault()
  const label = anchor.textContent?.trim() || '前往页面'
  followNavigation({ action: 'navigate', route: href, label })
}

async function openPanel(): Promise<void> {
  visible.value = true
  unreadAlerts.value = 0
  await nextTick()
  restoreOrAnchor()
  switcherRef.value?.ensureFreshOnOpen()
  await scrollToBottom()
}

function closePanel(): void {
  // 关闭前持久化运行状态,确保重开后能识别未完成会话(运行中/等待审批/等待输入)并跳回
  persistSnapshot()
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

async function retryRun(): Promise<void> {
  const runId = sessionRun.value?.run_id
  if (!runId || loading.value || !canRetryRun.value) return
  await runResponse({
    action: 'retry',
    surface: 'admin',
    session_id: sessionId.value,
    messages: conversationHistory(),
    run_id: runId,
  })
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

  /** 应用文本增量:剥离导航指令并渲染为可点击的「前往页面」确认按钮(不自动跳转) */
  const applyTextDelta = (): void => {
    const { cleaned, directives } = extractNavigateDirectives(rawText)
    const content = formatStreamContent(cleaned)
    if (!content.trim()) return
    showTyping.value = false
    if (!textTarget) {
      messages.value.push(assistantEntry({ type: 'text', content, status: 'completed' }))
      textTarget = messages.value[messages.value.length - 1]
      if (!visible.value) unreadAlerts.value += 1
    } else {
      textTarget.payload.content = content
    }
    textTarget.navigations = directives.length ? directives : undefined
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
        if (event.type === 'response.tool.started' && typeof event.tool_name === 'string' && event.tool_name) {
          lastActiveToolName.value = event.tool_name
        }
        if (!applyExistingTimelineToolEvent(event)) {
          applyResponseToolEvent(runToolCalls, event)
          syncTimeline()
        }
        syncBusy()
      } else if (event.type === 'response.output_text.delta') {
        rawText += event.delta
        applyTextDelta()
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
        lastActiveToolName.value = ''
        protocolError ||= eventErrorMessage(event)
        const terminalStatus = event.type === 'response.completed'
          ? 'completed'
          : event.type === 'response.cancelled' ? 'cancelled' : 'failed'
        if (sessionRun.value) sessionRun.value = { ...sessionRun.value, status: terminalStatus, error: protocolError }
        invalidateSessionPoll()
        syncBusy()
        persistSnapshot()
        const failed = event.type !== 'response.completed'
        const terminalError = failed ? protocolError : '响应已结束，但工具未返回完成事件'
        finishResponseToolCalls(
          runToolCalls,
          'failed',
          terminalError,
        )
        finishExistingTimelineToolCalls(activeRunId, terminalError)
        syncTimeline()
        // 导航不再自动跳转:PRISM_NAVIGATE 已渲染为「前往页面」按钮,由管理员点击确认,且不关闭面板。
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
    scheduleSessionPoll()
  }
}

async function sendMessage(): Promise<void> {
  const content = inputText.value.trim()
  if (!content || loading.value || sessionRestoring.value || sessionBusy.value) return
  messages.value.push(userEntry(content))
  inputText.value = ''
  // 新对话自动命名:首条用户消息提炼为会话标题
  if (autoTitleAgentChatSession('admin', sessionId.value, content)) {
    switcherRef.value?.reload?.()
  }
  await scrollToBottom()
  await runResponse({
    action: 'start',
    surface: 'admin',
    session_id: sessionId.value,
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

onBeforeUnmount(() => {
  meshBridge.stop()
  persistSnapshot()
  sessionPollStopped = true
  invalidateSessionPoll()
  activeResponse?.abort()
  window.removeEventListener('keydown', handleKeydown)
})

onMounted(() => meshBridge.start())
</script>

<template>
  <div class="admin-copilot" :class="{ 'is-open': visible }">
    <button
      v-if="!visible"
      class="copilot-trigger"
      :class="{ 'is-busy': mascotStatus !== 'idle' }"
      type="button"
      :aria-label="`打开${ASSISTANT_NAME}`"
      :title="ASSISTANT_NAME"
      @click="openPanel"
    >
      <PrismMascot :size="44" :status="mascotStatus !== 'idle' ? 'running' : 'idle'" />
      <span v-if="unreadAlerts" class="unread-dot" aria-label="有未读异常"></span>
    </button>

    <section
      v-else
      ref="panelRef"
      class="copilot-panel"
      :class="{ 'is-dragging': dragging, 'drag-over': dragActive }"
      :style="panelStyle"
      role="dialog"
      aria-label="管理副驾驶对话"
      @pointermove="moveDrag"
      @pointerup="endDrag"
      @pointercancel="endDrag"
      @dragover.prevent="onDragOver"
      @dragleave.prevent="onDragLeave"
      @drop.prevent="onDrop"
    >
      <div v-if="dragActive" class="drop-mask">
        <div class="drop-mask-text">松开鼠标,把文件交给小菱建项目</div>
      </div>
      <header class="copilot-header">
        <button class="panel-drag-handle" type="button" aria-label="移动管理副驾驶窗口" title="拖拽移动窗口" @pointerdown="beginDrag">
          ⠿
        </button>
        <div class="copilot-identity">
          <div class="copilot-avatar">
            <PrismMascot :size="30" :status="mascotStatus" />
          </div>
          <div class="copilot-title-block">
            <div class="copilot-title-line">
              <strong>{{ ASSISTANT_NAME }}</strong>
              <span class="copilot-run-badge" :class="`run-${mascotStatus}`"><i></i>{{ runStatusLabel }}</span>
              <span v-if="sessionPollError" class="copilot-sync-status is-error" role="status">{{ sessionPollError }}</span>
              <span v-else-if="sessionLastPolledAt" class="copilot-sync-status" role="status">已同步 {{ sessionLastPolledAt }}</span>
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
              class="copilot-session-switch"
              storage-key="admin"
              :legacy-key="LEGACY_SESSION_KEY"
              id-prefix="admin"
              :welcome-text="WELCOME_TEXT"
              :discover-remote="true"
              @select="handleSessionSelect"
              @sessions-changed="handleSwitcherReady"
            />
          </div>
        </div>
        <button class="icon-button" type="button" aria-label="收起管理副驾驶" title="收起" @click="closePanel">
          <el-icon><Close /></el-icon>
        </button>
      </header>

      <div v-if="mascotStatus === 'running' && lastActiveToolName" class="copilot-progress">
        正在执行 <code>{{ lastActiveToolName }}</code>
      </div>

      <div ref="messageArea" class="copilot-messages" aria-live="polite" @click="onMessageClick">
        <article
          v-for="entry in messages"
          :key="entry.id"
          class="message-row"
          :class="`is-${entry.role}`"
        >
          <div v-if="entry.role === 'assistant'" class="message-avatar">
            <PrismMascot :size="22" :status="'idle'" />
          </div>
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

            <!-- 页面引导:模型约定路由 + 指令导航按钮,鉴权由 AgentNavLink 同源守卫裁决 -->
            <div v-if="entry.role === 'assistant' && entry.navigations?.length" class="nav-directives">
              <AgentNavLink
                v-for="nav in entry.navigations"
                :key="nav.route"
                :href="nav.route"
                :label="nav.label || '前往对应管理页'"
                :hint="nav.hint"
                prominent
              />
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

        <AgentTeamTrace
          v-for="(team, index) in agentTeams"
          :key="team.team_id"
          :team="team"
          :loading="agentTeamLoading"
          :error="index === 0 ? agentTeamError : ''"
        />

        <div v-if="showTyping" class="typing-row">
          <div class="message-avatar">
            <PrismMascot :size="22" :status="'running'" />
          </div>
          <div class="typing-bubble"><i></i><i></i><i></i></div>
        </div>
      </div>

      <footer class="copilot-input-area">
        <div v-if="uploadStatus" class="upload-status">
          <span class="upload-status-spinner" />
          <span class="upload-status-text">{{ uploadStatus }}</span>
        </div>
        <div class="composer">
          <textarea
            v-model="inputText"
            rows="1"
            maxlength="2000"
            :placeholder="sessionRestoring ? '正在恢复 Agent 会话' : sessionBusy ? (isAgentResponseSessionWaiting(sessionRun?.status) ? '请先处理上方待办(审批/追问),或点击 + 新建对话' : '小菱正在运行中…可点击 + 新建对话并行处理') : '输入管理指令;也可直接拖入代码文件帮你建项目'"
            aria-label="输入管理指令"
            :disabled="loading || uploading || sessionRestoring || sessionBusy"
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
  position: relative;
  width: 60px;
  height: 60px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 50%;
  background: linear-gradient(145deg, #ffffff, #eef0fb);
  box-shadow: 0 8px 24px rgba(0, 110, 255, 0.24), 0 2px 6px rgba(15, 18, 34, 0.1);
  cursor: pointer;
  transition: transform 160ms ease, box-shadow 160ms ease;
}

.copilot-trigger:hover { transform: scale(1.06) translateY(-2px); box-shadow: 0 12px 30px rgba(0, 110, 255, 0.32), 0 3px 8px rgba(15, 18, 34, 0.12); }
.copilot-trigger.is-busy::after {
  content: '';
  position: absolute;
  inset: -3px;
  border-radius: 50%;
  border: 2px solid transparent;
  border-top-color: var(--agent-primary);
  border-right-color: #2ba471;
  animation: copilot-fab-spin 1.2s linear infinite;
}
@keyframes copilot-fab-spin { to { transform: rotate(360deg); } }
.unread-dot { position: absolute; top: 1px; right: 1px; width: 11px; height: 11px; border: 2px solid #fff; border-radius: 50%; background: var(--agent-danger); }

.copilot-panel {
  position: fixed;
  width: 400px;
  height: 620px;
  max-width: calc(100vw - 32px);
  max-height: calc(100dvh - 32px);
  display: grid;
  grid-template-areas:
    'header'
    'progress'
    'messages'
    'input';
  grid-template-rows: auto auto minmax(0, 1fr) auto;
  overflow: hidden;
  border: 1px solid rgba(0, 110, 255, 0.14);
  border-radius: 18px;
  background: var(--agent-bg);
  box-shadow: 0 18px 48px rgba(0, 60, 140, 0.16), 0 4px 14px rgba(15, 18, 34, 0.08);
}

.copilot-panel.is-dragging { user-select: none; }
.panel-drag-handle { display: grid; place-items: center; flex: 0 0 auto; width: 24px; height: 30px; margin-left: -10px; border: 0; border-radius: 6px; background: transparent; color: var(--agent-text-secondary); font-size: 18px; line-height: 1; cursor: grab; touch-action: none; }
.panel-drag-handle:hover { color: var(--agent-primary); background: rgba(0, 110, 255, 0.08); }
.copilot-panel.is-dragging .panel-drag-handle { cursor: grabbing; }
.copilot-header {
  grid-area: header;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 14px 10px 16px;
  border-bottom: 1px solid var(--agent-border);
  background: linear-gradient(135deg, rgba(0, 110, 255, 0.06), rgba(61, 188, 217, 0.05) 70%, transparent);
  /* 会话切换下拉(.session-menu)绝对定位在 header 内,不能被裁剪 */
  overflow: visible;
  position: relative;
  z-index: 6;
  border-radius: 18px 18px 0 0;
}
.copilot-identity { display: flex; align-items: center; gap: 10px; min-width: 0; }
.copilot-title-block { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.copilot-title-line { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.copilot-identity strong { display: block; font-size: 15px; line-height: 20px; }
.copilot-avatar,
.message-avatar { display: grid; place-items: center; flex: 0 0 auto; border-radius: 50%; background: linear-gradient(145deg, #f2f3ff, #e4ecfb); box-shadow: inset 0 0 0 1px rgba(0, 110, 255, 0.16); overflow: hidden; }
.copilot-avatar { width: 40px; height: 40px; }
.message-avatar { width: 26px; height: 26px; margin-top: 3px; }

.retry-run-btn {
  margin-left: 6px;
  padding: 1px 8px;
  border: 1px solid #d9dce0;
  border-radius: 999px;
  background: #fff;
  color: #1769d2;
  font-size: 11px;
  line-height: 18px;
  cursor: pointer;
}
.retry-run-btn:hover { border-color: #1769d2; background: #eef5ff; }
.copilot-run-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 10px;
  font-weight: 500;
  padding: 1px 7px;
  border-radius: 999px;
  border: 1px solid transparent;
}
.copilot-run-badge i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.copilot-run-badge.run-idle { color: var(--agent-text-secondary); background: #f2f3f5; border-color: var(--agent-border); }
.copilot-run-badge.run-running { color: #2f7a3d; background: rgba(43, 164, 113, 0.12); border-color: rgba(43, 164, 113, 0.35); }
.copilot-run-badge.run-running i { animation: copilot-run-blink 1s ease-in-out infinite; }
.copilot-run-badge.run-waiting { color: #b68039; background: rgba(217, 168, 87, 0.16); border-color: rgba(217, 168, 87, 0.4); }
.copilot-run-badge.run-waiting i { animation: copilot-run-blink 1s ease-in-out infinite; }
.copilot-sync-status { color: var(--agent-text-secondary); font-size: 9.5px; white-space: nowrap; }
.copilot-sync-status.is-error { color: var(--agent-danger); font-weight: 600; }
@keyframes copilot-run-blink { 0%, 100% { opacity: 0.35; } 50% { opacity: 1; } }

.copilot-progress {
  grid-area: progress;
  font-size: 11px;
  color: var(--agent-text-secondary);
  background: linear-gradient(90deg, rgba(0, 110, 255, 0.05), rgba(61, 188, 217, 0.05));
  border-bottom: 1px solid var(--agent-border);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.copilot-progress code {
  font-family: monospace;
  color: var(--agent-primary);
  font-size: 10.5px;
  background: rgba(0, 110, 255, 0.08);
  padding: 0 5px;
  border-radius: 3px;
}
.icon-button { width: 34px; height: 34px; display: grid; place-items: center; border: 0; border-radius: 50%; color: var(--agent-text-secondary); background: transparent; cursor: pointer; }
.icon-button:hover { color: var(--agent-text); background: #f2f3f5; }

.copilot-messages { grid-area: messages; min-height: 0; overflow-y: auto; padding: 16px 14px; background: #f7f8fa; }
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
.message-bubble.markdown-body :deep(ul) { list-style: none; padding-left: 0; margin: 0; }
.message-bubble.markdown-body :deep(li) { margin: 2px 0; }
/* 站内页面引导链接:品牌色导航样式,未授权目标已由守卫隐藏 */
.message-bubble.markdown-body :deep(a) {
  color: var(--agent-primary, #5b58e8);
  font-weight: 600;
  text-decoration: none;
  border-bottom: 1px dashed var(--agent-primary, #5b58e8);
  cursor: pointer;
}
.nav-directives {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
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

.copilot-panel.drag-over::after {
  content: '';
  position: absolute;
  inset: 0;
  z-index: 19;
  border-radius: inherit;
  background: rgba(91, 88, 232, 0.06);
  pointer-events: none;
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
  border-radius: inherit;
}
.drop-mask-text {
  padding: 14px 22px;
  border-radius: 12px;
  background: #f5f6ff;
  border: 1.5px dashed #8f8cf0;
  color: #5b58e8;
  font-size: 14px;
  font-weight: 600;
}
.upload-status {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  margin-bottom: 2px;
  border-radius: 8px;
  background: #f5f6ff;
  border: 1px solid #d4d2f8;
  font-size: 12.5px;
  color: #5b58e8;
}
.upload-status-spinner {
  width: 12px;
  height: 12px;
  border: 2px solid #d4d2f8;
  border-top-color: #5b58e8;
  border-radius: 50%;
  animation: upload-spin 0.8s linear infinite;
  flex-shrink: 0;
}
@keyframes upload-spin {
  to { transform: rotate(360deg); }
}
.upload-status-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.copilot-input-area { grid-area: input; border-top: 1px solid var(--agent-border); background: #fff; border-radius: 0 0 18px 18px; }
.composer { display: grid; grid-template-columns: minmax(0, 1fr) 38px; align-items: end; gap: 8px; padding: 9px 10px 10px; }
.composer textarea { min-height: 38px; max-height: 84px; resize: none; padding: 9px 10px; border: 1px solid #d8dade; border-radius: 7px; color: var(--agent-text); outline: none; line-height: 18px; }
.composer textarea:focus { border-color: var(--agent-primary); box-shadow: 0 0 0 2px rgba(0, 110, 255, 0.1); }
.send-button { width: 38px; height: 38px; display: grid; place-items: center; border: 0; border-radius: 50%; color: #fff; background: var(--agent-primary); cursor: pointer; }

@media (max-width: 520px) {
  .admin-copilot { right: 12px; bottom: 12px; left: 12px; }
  .copilot-trigger { margin-left: auto; }
  .copilot-panel { width: 100%; height: min(620px, calc(100dvh - 24px)); max-width: none; max-height: none; }
}
</style>
