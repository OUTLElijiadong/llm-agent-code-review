<script setup lang="ts">
import { computed, getCurrentInstance, inject, reactive, ref, watch } from 'vue'
import { usePreferredReducedMotion } from '@vueuse/core'
import { CircleCheck, WarningFilled } from '@element-plus/icons-vue'

import { toolDisplayInfo } from '@/utils/toolDisplay'
import { formatResponseValue, type ResponseToolCall, type ResponseToolCallStatus } from '@/utils/responsesTimeline'
import { messageActivityExpandedKey } from '@/components/ai/agentActivityDisclosure'

/**
 * 「小菱工作步骤」通俗时间线。
 *
 * 设计原则(尼尔森·系统状态可见 + 游戏3原则·清晰度):
 * - 不展示代码级调用链:工具名/状态机术语全部翻译成人话动作
 * - 只为真实新增步骤和主动展开的结果提供一次入场提示
 * - 页面操作类显示「正在帮你操作」+ 彩点,呼应全屏彩框/虚拟鼠标
 * - 运行、等待、取消按真实事件区分；失败原因立即可见，撤回内容不做离场
 */
const props = withDefaults(defineProps<{
  calls: ResponseToolCall[]
  /** 审计四阶段进度(DeepAudit 式角色叙事:侦察员→分析师→验证员→汇报员)。 */
  auditPhases?: Array<{ phase: string; label: string; message: string }>
  /** 管理端与成员端共用小菱身份；surface 只分隔会话和权限。 */
  subject?: string
  /** 只有归属当前活跃 run 的审计阶段才显示活动标记。 */
  active?: boolean
}>(), {
  subject: '小菱',
  active: false,
})

const reducedMotion = usePreferredReducedMotion()
const componentId = getCurrentInstance()?.uid ?? 'unknown'
const resultIdPrefix = `tool-result-${componentId}`
const stepsPanelId = `xl-steps-panel-${componentId}`
const expanded = ref(false)
const parentExpanded = inject(messageActivityExpandedKey, null)
const embedded = computed(() => parentExpanded !== null)
const detailsExpanded = computed(() => parentExpanded?.value ?? expanded.value)
const resultId = (key: string): string => `${resultIdPrefix}-${encodeURIComponent(key)}`
/** 内容撤回不等待 CSS 过渡，尤其是权限变化和错误替换。 */
function removeImmediately(_element: Element, done: () => void): void { done() }

const STATUS_NOTES: Record<ResponseToolCallStatus, string> = {
  streaming: '正在准备这个操作…',
  queued: '排队等候中…',
  delivered: '排队等候中…',
  acknowledged: '即将开始…',
  processing: '正在进行…',
  running: '正在进行…',
  waiting_approval: '等你确认后继续',
  waiting_input: '在等你的回答',
  completed: '做好了',
  failed: '没做成',
  rejected: '已按你的要求取消',
}

const ACTIVE_STATUSES = new Set<ResponseToolCallStatus>([
  'streaming', 'queued', 'delivered', 'acknowledged', 'processing', 'running',
])

function supervisionLabel(decision: 'allow' | 'escalate' | 'deny', risk: 'low' | 'medium' | 'high' | 'critical'): string {
  if (decision === 'deny') return '监督阻断'
  if (decision === 'escalate') return '监督待确认'
  if (risk === 'high' || risk === 'critical') return '监督规则异常'
  if (risk === 'low') return '低风险自动通过'
  return '中风险自动通过'
}

const visibleCalls = computed(() => props.calls.filter((call) => call.name || call.argumentsText))

interface StepView {
  key: string
  /** 人话动作:如「检索知识库」「帮你创建项目」。 */
  action: string
  /** 当前一步的人话状态。 */
  note: string
  status: ResponseToolCallStatus
  isRag: boolean
  isPageAction: boolean
  running: boolean
  done: boolean
  failed: boolean
  waiting: boolean
  supervision?: {
    label: string
    riskLevel: 'low' | 'medium' | 'high' | 'critical'
    reason: string
  }
  error?: string
  result?: string
}

function stepView(call: ResponseToolCall): StepView {
  const info = toolDisplayInfo(call.name)
  const subject = call.subject?.trim()
  const status = call.status
  return {
    key: call.key,
    action: subject || info.label,
    note: STATUS_NOTES[status] ?? '处理中',
    status,
    isRag: info.isRag,
    isPageAction: info.isPageAction,
    running: ACTIVE_STATUSES.has(status),
    done: status === 'completed',
    failed: status === 'failed',
    waiting: status === 'waiting_approval' || status === 'waiting_input',
    supervision: call.supervision ? {
      label: supervisionLabel(call.supervision.decision, call.supervision.riskLevel),
      riskLevel: call.supervision.riskLevel,
      reason: call.supervision.reason,
    } : undefined,
    error: status === 'failed' ? formatResponseValue(call.error) || undefined : undefined,
    // Mesh 的 preview 是传输元数据，不是用户请求的工具输出。
    result: status === 'completed' && !call.direction ? formatResponseValue(call.resultPreview) || undefined : undefined,
  }
}

const steps = computed<StepView[]>(() => visibleCalls.value.map(stepView))
const doneCount = computed(() => steps.value.filter((step) => step.done).length)
const failedCount = computed(() => steps.value.filter((step) => step.status === 'failed').length)
const failedSteps = computed(() => steps.value.filter((step) => step.failed))
/** 是否有 RAG 检索正在/曾经发生(顶部显示检索徽标)。 */
const ragActive = computed(() => steps.value.some((step) => step.isRag && step.running))
const pageActionActive = computed(() => steps.value.some((step) => step.isPageAction && step.running))

/** 单项结果按需展开；失败名称在折叠摘要中保留，错误详情随步骤面板展开。 */
const expandedKeys = reactive(new Set<string>())
// 使用默认 pre：工具处理器先 push 再补齐状态，同步读取会缓存尚未补齐的中间态。
watch(steps, (current) => {
  const keys = new Set(current.filter((step) => step.result).map((step) => step.key))
  for (const key of expandedKeys) if (!keys.has(key)) expandedKeys.delete(key)
})

function toggle(step: StepView): void {
  if (expandedKeys.has(step.key)) expandedKeys.delete(step.key)
  else expandedKeys.add(step.key)
}

function summaryText(): string {
  const total = steps.value.length
  const latestPhase = props.auditPhases?.at(-1)
  const activePhase = props.active && latestPhase ? `当前：${latestPhase.label}` : ''
  if (!total) {
    const phaseSummary = props.auditPhases?.length ? `${props.auditPhases.length} 个审计阶段` : ''
    return [phaseSummary, activePhase].filter(Boolean).join(' · ')
  }
  const parts: string[] = []
  if (doneCount.value) parts.push(`${doneCount.value} 步完成`)
  const active = steps.value.filter((step) => step.running).length
  if (active > 0) parts.push(`${active} 步进行中`)
  const waiting = steps.value.filter((step) => step.waiting).length
  if (waiting) parts.push(`${waiting} 步待确认`)
  const cancelled = steps.value.filter((step) => step.status === 'rejected').length
  if (cancelled) parts.push(`${cancelled} 步已取消`)
  if (failedCount.value) parts.push(`${failedCount.value} 步出错`)
  if (activePhase) parts.push(activePhase)
  return parts.join(' · ')
}

const toggleLabel = computed(() => {
  const summary = summaryText()
  return `${detailsExpanded.value ? '收起' : '展开'}${props.subject}的工作步骤${summary ? `，${summary}` : ''}`
})

function toggleTimeline(): void {
  expanded.value = !expanded.value
}
</script>

<template>
  <section v-if="steps.length || auditPhases?.length" class="xl-steps" :aria-label="`${subject}工作步骤`">
    <header v-if="!embedded" class="xl-steps-head">
      <button
        class="xl-steps-toggle"
        type="button"
        :aria-expanded="detailsExpanded"
        :aria-controls="stepsPanelId"
        :aria-label="toggleLabel"
        @click="toggleTimeline"
      >
        <span class="xl-steps-caret" :class="{ 'is-open': detailsExpanded }" aria-hidden="true">›</span>
        <span class="xl-steps-title">{{ subject }}的工作</span>
        <span v-if="ragActive" class="xl-steps-rag" role="status">
          <i class="xl-rag-pulse" aria-hidden="true"></i>检索知识库中…
        </span>
        <span v-else-if="pageActionActive" class="xl-steps-page" role="status">
          <i class="xl-page-pulse" aria-hidden="true"></i>正在帮你操作页面
        </span>
        <span class="xl-steps-summary" role="status" aria-live="polite">{{ summaryText() }}</span>
        <span v-if="failedSteps.length" class="xl-steps-collapsed-errors" role="status" aria-label="失败步骤">
          <span v-for="step in failedSteps" :key="step.key" class="xl-steps-collapsed-error">出错：{{ step.action }}</span>
        </span>
      </button>
    </header>

    <div :id="stepsPanelId" v-show="detailsExpanded" class="xl-steps-detail">
      <TransitionGroup v-if="auditPhases?.length" tag="ol" name="xl-arrival" :css="reducedMotion !== 'reduce'" class="xl-audit-phases" aria-label="审计阶段" @leave="removeImmediately">
        <li v-for="(item, index) in auditPhases" :key="item.phase" class="xl-audit-phase" :class="{ 'is-latest': active && index === auditPhases.length - 1 }">
          <span class="xl-audit-idx">{{ index + 1 }}</span>
          <span class="xl-audit-label">{{ item.label }}</span>
          <span v-if="active && index === auditPhases.length - 1" class="xl-audit-now">进行中</span>
        </li>
      </TransitionGroup>

      <TransitionGroup v-if="steps.length" tag="ol" name="xl-arrival" :css="reducedMotion !== 'reduce'" class="xl-step-list" @leave="removeImmediately">
        <li
          v-for="step in steps"
          :key="step.key"
          class="xl-step"
          :class="{
            'is-done': step.done,
            'is-failed': step.failed,
            'is-running': step.running,
            'is-waiting': step.waiting,
            'is-rag': step.isRag,
          }"
        >
          <div class="xl-step-node" aria-hidden="true">
            <template v-if="step.done">
              <el-icon class="xl-step-check"><CircleCheck /></el-icon>
            </template>
            <template v-else-if="step.failed">
              <el-icon class="xl-step-warn"><WarningFilled /></el-icon>
            </template>
            <template v-else-if="step.isRag && step.running">
              <i class="xl-step-rag-dot"></i>
            </template>
            <template v-else-if="step.running">
              <i class="xl-step-spinner"></i>
            </template>
            <template v-else>
              <i class="xl-step-dot"></i>
            </template>
          </div>

          <div class="xl-step-body">
            <div class="xl-step-line">
              <span class="xl-step-action">{{ step.action }}</span>
              <span v-if="step.isPageAction" class="xl-step-chip is-page" title="小菱正在替你操作页面">帮我操作</span>
              <span v-else-if="step.isRag" class="xl-step-chip is-rag">知识库</span>
              <span
                v-if="step.supervision"
                class="xl-step-chip is-supervised"
                :class="`risk-${step.supervision.riskLevel}`"
                :title="step.supervision.reason"
                aria-label="小菱监督子 Agent 风险复核"
              >{{ step.supervision.label }}</span>
              <span class="xl-step-note" :class="{ 'is-waiting': step.waiting }">{{ step.note }}</span>
            </div>
            <div v-if="step.error" class="xl-step-error" role="status">{{ step.error }}</div>
            <button v-if="step.result" type="button" class="xl-result-toggle" :aria-label="`${step.action}：${expandedKeys.has(step.key) ? '收起结果' : '查看结果'}`" :aria-expanded="expandedKeys.has(step.key)" :aria-controls="resultId(step.key)" @click="toggle(step)">
              {{ expandedKeys.has(step.key) ? '收起结果' : '查看结果' }}
            </button>
            <Transition name="xl-result" :css="reducedMotion !== 'reduce'" @leave="removeImmediately">
              <pre v-if="step.result && expandedKeys.has(step.key)" :id="resultId(step.key)" class="xl-step-result" tabindex="0" :aria-label="`${step.action}的结果`">{{ step.result }}</pre>
            </Transition>
          </div>
        </li>
      </TransitionGroup>
    </div>
  </section>
</template>

<style scoped>
.xl-steps {
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  margin-top: 8px;
  overflow: hidden;
  border: 1px solid var(--gray-200);
  border-radius: 10px;
  background: #fff;
  color: var(--gray-800);
  font-size: 12px;
}

.xl-steps-head {
  min-width: 0;
  border-bottom: 1px solid var(--gray-100);
}
.xl-steps-toggle { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; width: 100%; min-width: 0; padding: 8px 12px; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.xl-steps-toggle:focus-visible { outline: 2px solid var(--brand-500); outline-offset: -2px; }
.xl-steps-title { flex: none; font-weight: 650; }
.xl-steps-caret { flex: none; color: var(--gray-400); font-size: 18px; line-height: 14px; transform: rotate(0); transition: transform 150ms ease; }
.xl-steps-caret.is-open { transform: rotate(90deg); }
.xl-steps-summary { margin-left: auto; color: var(--gray-500); font-size: 10.5px; white-space: nowrap; }
.xl-steps-collapsed-errors { display: flex; flex: 1 0 100%; flex-wrap: wrap; justify-content: flex-start; gap: 3px 6px; min-width: 0; margin-left: 26px; color: var(--color-danger); font-size: 10px; }
.xl-steps-collapsed-error { overflow-wrap: anywhere; }
.xl-steps-detail { min-width: 0; }

.xl-steps-rag, .xl-steps-page {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 10.5px;
  font-weight: 600;
}
.xl-steps-rag { color: var(--accent-600); background: var(--accent-50); }
.xl-steps-page { color: var(--brand-600); background: var(--brand-50); }

.xl-rag-pulse, .xl-page-pulse {
  width: 7px; height: 7px; border-radius: 50%;
}
.xl-rag-pulse { background: var(--accent-500); }
.xl-page-pulse { background: var(--brand-500); }

.xl-step-list { display: grid; margin: 0; padding: 6px 12px; list-style: none; }

/* ── 审计四阶段角色卡(DeepAudit 式叙事) ───────────── */
.xl-audit-phases { display: grid; gap: 3px; margin: 0; padding: 8px 12px; list-style: none; }
.xl-audit-phase {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  padding: 4px 8px;
  border-radius: 7px;
  background: var(--gray-50);
}
.xl-audit-phase.is-latest:last-child {
  background: linear-gradient(90deg, rgba(107, 124, 255, 0.10), rgba(75, 155, 255, 0.08));
}
.xl-audit-idx {
  display: grid;
  place-items: center;
  flex: none;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--brand-100);
  color: var(--brand-700);
  font-size: 10px;
  font-weight: 700;
}
.xl-audit-label {
  min-width: 0;
  color: var(--gray-700);
  font-size: 11.5px;
  font-weight: 600;
  overflow-wrap: anywhere;
}
.xl-audit-now {
  margin-left: auto;
  flex: none;
  color: var(--brand-600);
  font-size: 10px;
  font-weight: 600;
}

.xl-step {
  display: flex;
  gap: 9px;
  min-width: 0;
  padding: 5px 0;
}
.xl-step + .xl-step { border-top: 1px dashed var(--gray-100); }

.xl-step-node { display: grid; place-items: center; flex: none; width: 18px; height: 18px; margin-top: 1px; }
.xl-step-check { color: var(--color-success); font-size: 15px; }
.xl-step-warn { color: var(--color-danger); font-size: 15px; }

.xl-step-spinner {
  width: 12px; height: 12px;
  border: 2px solid var(--brand-100);
  border-top-color: var(--brand-500);
  border-radius: 50%;
  animation: xl-spin 0.9s linear infinite;
}
@keyframes xl-spin { to { transform: rotate(360deg); } }

/* RAG 检索专属:青色双点交替 */
.xl-step-rag-dot {
  position: relative;
  width: 10px; height: 10px;
}
.xl-step-rag-dot::before, .xl-step-rag-dot::after {
  content: '';
  position: absolute;
  top: 50%;
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--accent-500);
  transform: translateY(-50%);
}
.xl-step-rag-dot::before { left: 0; animation: xl-rag-left 1s ease-in-out infinite; }
.xl-step-rag-dot::after { right: 0; background: var(--accent-300); animation: xl-rag-right 1s ease-in-out infinite; }
@keyframes xl-rag-left { 0%, 100% { opacity: 1; } 50% { opacity: 0.25; } }
@keyframes xl-rag-right { 0%, 100% { opacity: 0.25; } 50% { opacity: 1; } }

.xl-step-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--gray-300);
}

.xl-step-body { flex: 1; min-width: 0; }
.xl-step-line { display: flex; align-items: baseline; gap: 6px; min-width: 0; flex-wrap: wrap; }
.xl-step-action {
  min-width: 0;
  color: var(--gray-800);
  font-weight: 600;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.xl-step.is-running .xl-step-action { color: var(--brand-700); }
.xl-step.is-failed .xl-step-action { color: var(--color-danger); }

.xl-step-chip {
  flex: none;
  padding: 0 6px;
  border-radius: 999px;
  font-size: 9.5px;
  line-height: 15px;
}
.xl-step-chip.is-page { background: var(--brand-50); color: var(--brand-600); }
.xl-step-chip.is-rag { background: var(--accent-50); color: var(--accent-600); }
.xl-step-chip.is-supervised { border: 1px solid transparent; }
.xl-step-chip.is-supervised.risk-low { background: var(--color-success-soft, #eef9f2); color: var(--color-success); }
.xl-step-chip.is-supervised.risk-medium { background: var(--brand-50); color: var(--brand-700); }
.xl-step-chip.is-supervised.risk-high,
.xl-step-chip.is-supervised.risk-critical { background: var(--color-danger-soft, #fff1f0); color: var(--color-danger); }

.xl-step-note {
  margin-left: auto;
  flex: none;
  color: var(--gray-500);
  font-size: 10.5px;
  white-space: nowrap;
}
.xl-step.is-running .xl-step-note { color: var(--brand-600); }
.xl-step-note.is-waiting { color: var(--sev-medium); font-weight: 600; }

.xl-step-error {
  margin-top: 3px;
  color: var(--color-danger);
  font-size: 10.5px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.xl-result-toggle { margin-top: 4px; padding: 2px 4px; border: 0; border-radius: 4px; background: transparent; color: var(--brand-700); font: inherit; font-size: 11px; cursor: pointer; }
.xl-result-toggle:focus-visible { outline: 2px solid var(--brand-500); outline-offset: 2px; }
.xl-step-result { max-height: 180px; overflow: auto; margin: 5px 0 0; padding: 8px; border-radius: 6px; background: var(--gray-50); white-space: pre-wrap; overflow-wrap: anywhere; font-size: 11px; line-height: 1.5; }
/* 只进入、不延迟离开；无 appear，恢复历史直接呈现。 */
.xl-arrival-enter-active, .xl-result-enter-active { transition: opacity 160ms ease-out, transform 160ms ease-out; }
.xl-arrival-enter-from, .xl-result-enter-from { opacity: 0; transform: translateY(4px); }
/* 新增失败或入场中失败都立即显示，不被父步骤的淡入遮住。 */
.xl-step.is-failed { opacity: 1; transform: none; transition: none; }
@media (prefers-reduced-motion: reduce) {
  .xl-step-spinner, .xl-step-rag-dot::before, .xl-step-rag-dot::after, .xl-steps-caret { animation: none; transition: none; }
  .xl-arrival-enter-active, .xl-result-enter-active { transition: none; }
  .xl-arrival-enter-from, .xl-result-enter-from { opacity: 1; transform: none; }
}
</style>
