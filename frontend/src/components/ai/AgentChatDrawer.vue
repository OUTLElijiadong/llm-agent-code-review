<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Close, Promotion } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import MarkdownIt from 'markdown-it'
import dayjs from 'dayjs'
import { post } from '@/api/http'
import { subscribeAgentEvents } from '@/utils/agentEventStream'
import { getProjects } from '@/api/project'
import { getReviewTasks } from '@/api/review'
import AgentAvatar from '@/components/agent/AgentAvatar.vue'
import type { AgentEvent, ClarifyPayload, ClarifyQuestion } from '@/types/agentEvent'
import type { AgentStatus } from '@/types/agent'

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
  role: 'user' | 'assistant'
  content: string
  time: string
  trace_id?: string
  steps?: StepBubble[]
  clarify?: ClarifyPayload
  /** v3.0 双层调度调用链(空表示未触发双层调度) */
  planSteps?: PlanStep[]
}

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [value: boolean] }>()

const messages = ref<ChatMessage[]>([])
const inputText = ref('')
const loading = ref(false)
const modelName = ref('deepseek-v4-flash')
const chatBody = ref<HTMLElement>()

const md = new MarkdownIt({ breaks: true })

// === SSE: 全局事件流 ===
let stream: ReturnType<typeof subscribeAgentEvents> | null = null
const eventsByTrace = ref<Record<string, StepBubble[]>>({})

function ensureStream(): void {
  if (stream) return
  stream = subscribeAgentEvents((ev: AgentEvent) => {
    if (!ev.trace_id) return
    const bubble: StepBubble = {
      agent: ev.agent,
      type: ev.type,
      message: ev.message,
      time: dayjs(ev.timestamp).format('HH:mm:ss'),
    }
    const list = eventsByTrace.value[ev.trace_id] ?? []
    list.push(bubble)
    eventsByTrace.value[ev.trace_id] = list
    // 把步骤同步到对应消息
    const target = messages.value.find((m) => m.trace_id === ev.trace_id)
    if (target) target.steps = [...list]
  }, {
    replay: 0,
    onError: () => {
      stream = null
    },
  })
}

function teardownStream(): void {
  stream?.close()
  stream = null
}

// === Clarify 选项加载 ===
const projectOptions = ref<{ value: number; label: string }[]>([])
const taskOptions = ref<{ value: number; label: string }[]>([])

let optionsPromise: Promise<void> | null = null

async function ensureProjectOptions(): Promise<void> {
  if (projectOptions.value.length) return
  if (optionsPromise) return optionsPromise
  optionsPromise = (async () => {
    try {
      const data = await getProjects({ page: 1, page_size: 100 })
      projectOptions.value = (data.items ?? []).map((p) => ({
        value: p.id,
        label: `#${p.id} ${p.project_name}`,
      }))
    } catch {
      projectOptions.value = []
    } finally {
      optionsPromise = null
    }
  })()
  return optionsPromise
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

function ensureClarifyAnswers(clarifyId: string, questions: ClarifyQuestion[]): void {
  if (!clarifyAnswers.value[clarifyId]) {
    const init: Record<string, string | number> = {}
    // v3.1: 后端模糊命中项目时会下发 default,预填后用户一键即可确认
    for (const q of questions) init[q.key] = q.default ?? ''
    clarifyAnswers.value[clarifyId] = init
  }
  // 仅当后端未随问题下发候选项时,才回退到前端二次拉取列表
  for (const q of questions) {
    if (q.type === 'select_project' && !(q.options && q.options.length)) void ensureProjectOptions()
    if (q.type === 'select_task' && !(q.options && q.options.length)) void ensureTaskOptions()
  }
}

async function submitClarify(message: ChatMessage): Promise<void> {
  const clarify = message.clarify
  if (!clarify) return
  const answers = clarifyAnswers.value[clarify.clarify_id] ?? {}
  const missing = clarify.questions.find((q) => q.required && !answers[q.key])
  if (missing) {
    ElMessage.warning(`请先回答: ${missing.label}`)
    return
  }
  loading.value = true
  try {
    const res = await post<{ content: string; clarify?: ClarifyPayload; model?: string }>(
      '/agents/clarify',
      { clarify_id: clarify.clarify_id, answers },
    )
    message.clarify = undefined
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
  if (!text || loading.value) return

  const traceId = `c_${Math.random().toString(36).slice(2, 10)}_${Date.now().toString(36)}`
  ensureStream()
  eventsByTrace.value[traceId] = []
  messages.value.push({ role: 'user', content: text, time: dayjs().format('HH:mm') })
  inputText.value = ''
  loading.value = true

  await nextTick()
  scrollToBottom()

  const history = messages.value
    .filter((m) => !m.clarify)
    .slice(0, -1)
    .map((m) => ({ role: m.role, content: m.content }))
  history.push({ role: 'user', content: text })

  try {
    const res = await post<{
      content: string
      model: string
      trace_id?: string
      clarify?: ClarifyPayload | null
      plan_steps?: PlanStep[] | null
    }>('/ai/chat', { messages: history, stream: false, trace_id: traceId })

    modelName.value = res.model || 'deepseek-v4-flash'
    const tid = res.trace_id || traceId
    messages.value.push({
      role: 'assistant',
      content: res.content,
      time: dayjs().format('HH:mm'),
      trace_id: tid,
      steps: eventsByTrace.value[tid] ?? [],
      clarify: res.clarify ?? undefined,
      planSteps: res.plan_steps ?? undefined,
    })
    if (res.clarify) {
      ensureClarifyAnswers(res.clarify.clarify_id, res.clarify.questions)
    }
  } catch {
    messages.value.push({
      role: 'assistant',
      content: '抱歉,Agent 助手暂时无法响应,请稍后重试。',
      time: dayjs().format('HH:mm'),
    })
  } finally {
    loading.value = false
    await nextTick()
    scrollToBottom()
  }
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
    ensureStream()
    void ensureProjectOptions()
    void ensureTaskOptions()
    if (messages.value.length === 0) {
      messages.value.push({
        role: 'assistant',
        content:
          '你好!我是 PRISM 平台的 Agent 助手,由 DeepSeek V4 驱动。\n\n'
          + '我会**实时展示**自己每一步的调度过程,遇到不确定的字段会**主动追问**,'
          + '不会基于猜测做决定。可以问我项目列表、启动审查、生成 AI 修复提示词等。',
        time: dayjs().format('HH:mm'),
      })
    }
    nextTick(() => scrollToBottom())
  } else {
    teardownStream()
  }
})

const supportPrefill = computed(() => true)
function handlePrefill(e: CustomEvent<{ prefill?: string }>): void {
  if (!props.visible) emit('update:visible', true)
  if (e.detail?.prefill) inputText.value = e.detail.prefill
}

if (typeof window !== 'undefined') {
  window.addEventListener('prism:open-agent-chat', handlePrefill as EventListener)
}

onBeforeUnmount(() => {
  teardownStream()
  if (typeof window !== 'undefined') {
    window.removeEventListener('prism:open-agent-chat', handlePrefill as EventListener)
  }
})
</script>

<template>
  <Teleport to="body">
    <Transition name="drawer">
      <div v-if="visible" class="chat-overlay" @click.self="close">
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
            <div v-for="(msg, i) in messages" :key="i" class="msg-row" :class="msg.role">
              <div class="msg-avatar">{{ msg.role === 'user' ? 'U' : 'AG' }}</div>
              <div class="msg-bubble">
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
                  v-if="msg.role === 'assistant'"
                  class="msg-content markdown-body"
                  v-html="md.render(msg.content)"
                />
                <div v-else class="msg-content">{{ msg.content }}</div>

                <!-- Clarify 主动追问表单 -->
                <div v-if="msg.clarify" class="clarify-card">
                  <header class="clarify-head">
                    <span class="clarify-icon">🤔</span>
                    <span>请补充以下信息,我再继续执行</span>
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
                      allow-create
                      default-first-option
                      placeholder="选择或输入项目 ID"
                      style="width: 100%"
                    >
                      <el-option
                        v-for="opt in (q.options && q.options.length ? q.options : projectOptions)"
                        :key="String(opt.value)"
                        :label="opt.label"
                        :value="opt.value"
                      />
                    </el-select>
                    <el-select
                      v-else-if="q.type === 'select_task'"
                      v-model="clarifyAnswers[msg.clarify.clarify_id][q.key]"
                      size="small"
                      filterable
                      allow-create
                      default-first-option
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
                    <el-button
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

            <div v-if="loading" class="msg-row assistant">
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
              placeholder="输入问题,Enter 发送,Shift+Enter 换行"
              rows="2"
              :disabled="loading"
              @keydown="handleKeydown"
            />
            <button
              class="send-btn"
              type="button"
              :disabled="loading || !inputText.trim()"
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
  inset: 0;
  z-index: 3000;
  background: rgba(0, 0, 0, 0.35);
  display: flex;
  justify-content: flex-end;
}

.chat-drawer {
  width: 460px;
  max-width: 92vw;
  height: 100%;
  background: #fff;
  display: flex;
  flex-direction: column;
  box-shadow: -4px 0 24px rgba(0, 0, 0, 0.12);
}

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
  border-radius: 10px;
  padding: 12px 14px;
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

.clarify-foot {
  display: flex;
  justify-content: flex-end;
  margin-top: 6px;
}

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
.drawer-leave-to .chat-drawer { transform: translateX(100%); }
</style>
