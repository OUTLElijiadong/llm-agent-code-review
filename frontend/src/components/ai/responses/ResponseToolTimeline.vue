<script setup lang="ts">
import { computed, reactive } from 'vue'
import { CircleCheck, WarningFilled } from '@element-plus/icons-vue'

import { toolDisplayInfo } from '@/utils/toolDisplay'
import type { ResponseToolCall, ResponseToolCallStatus } from '@/utils/responsesTimeline'

/**
 * 「小菱工作步骤」通俗时间线。
 *
 * 设计原则(尼尔森·系统状态可见 + 游戏3原则·清晰度):
 * - 不展示代码级调用链:工具名/状态机术语全部翻译成人话动作
 * - RAG 检索类显示专属「检索中」脉冲态,让用户知道小菱在翻知识库
 * - 页面操作类显示「正在帮你操作」+ 彩点,呼应全屏彩框/虚拟鼠标
 * - 进行中的步骤高亮呼吸,完成的收成对勾,失败的给原因
 */
const props = withDefaults(defineProps<{
  calls: ResponseToolCall[]
  /** 审计四阶段进度(DeepAudit 式角色叙事:侦察员→分析师→验证员→汇报员)。 */
  auditPhases?: Array<{ phase: string; label: string; message: string }>
  /** 助手称谓: 成员端小菱 / 管理端贾维斯(角色分离, 默认小菱)。 */
  subject?: string
}>(), {
  subject: '小菱',
})

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
  error?: string
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
    failed: status === 'failed' || status === 'rejected',
    waiting: status === 'waiting_approval' || status === 'waiting_input',
    error: call.error?.trim() || undefined,
  }
}

const steps = computed<StepView[]>(() => visibleCalls.value.map(stepView))
const doneCount = computed(() => steps.value.filter((step) => step.done).length)
const failedCount = computed(() => steps.value.filter((step) => step.failed).length)
/** 是否有 RAG 检索正在/曾经发生(顶部显示检索徽标)。 */
const ragActive = computed(() => steps.value.some((step) => step.isRag && (step.running || step.waiting)))
const pageActionActive = computed(() => steps.value.some((step) => step.isPageAction && step.running))

/** 单条展开状态;默认折叠,失败步骤自动展开让用户直接看到原因。 */
const expandedKeys = reactive(new Set<string>())
for (const step of steps.value) {
  if (step.failed && step.error) expandedKeys.add(step.key)
}

function toggle(step: StepView): void {
  if (expandedKeys.has(step.key)) expandedKeys.delete(step.key)
  else expandedKeys.add(step.key)
}

function summaryText(): string {
  const total = steps.value.length
  if (!total) return ''
  const parts: string[] = []
  if (doneCount.value) parts.push(`${doneCount.value} 步完成`)
  const active = total - doneCount.value - failedCount.value
  if (active > 0) parts.push(`${active} 步进行中`)
  if (failedCount.value) parts.push(`${failedCount.value} 步出错`)
  return parts.join(' · ')
}
</script>

<template>
  <section v-if="steps.length || auditPhases?.length" class="xl-steps" aria-label="小菱工作步骤">
    <header class="xl-steps-head">
      <span class="xl-steps-title">{{ subject }}的工作</span>
      <span v-if="ragActive" class="xl-steps-rag" role="status">
        <i class="xl-rag-pulse" aria-hidden="true"></i>检索知识库中…
      </span>
      <span v-else-if="pageActionActive" class="xl-steps-page" role="status">
        <i class="xl-page-pulse" aria-hidden="true"></i>正在帮你操作页面
      </span>
      <span class="xl-steps-summary">{{ summaryText() }}</span>
    </header>

    <ol v-if="auditPhases?.length" class="xl-audit-phases" aria-label="审计阶段">
      <li v-for="(item, index) in auditPhases" :key="item.phase" class="xl-audit-phase is-latest">
        <span class="xl-audit-idx">{{ index + 1 }}</span>
        <span class="xl-audit-label">{{ item.label }}</span>
        <span v-if="index === (auditPhases?.length ?? 0) - 1" class="xl-audit-now">进行中</span>
      </li>
    </ol>

    <ol v-if="steps.length" class="xl-step-list">
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

        <div
          class="xl-step-body"
          :class="{ 'is-expandable': Boolean(step.error) }"
          :role="step.error ? 'button' : undefined"
          :tabindex="step.error ? 0 : undefined"
          @click="step.error && toggle(step)"
          @keydown.enter.prevent="step.error && toggle(step)"
          @keydown.space.prevent="step.error && toggle(step)"
        >
          <div class="xl-step-line">
            <span class="xl-step-action">{{ step.action }}</span>
            <span v-if="step.isPageAction" class="xl-step-chip is-page" title="小菱正在替你操作页面">帮我操作</span>
            <span v-else-if="step.isRag" class="xl-step-chip is-rag">知识库</span>
            <span class="xl-step-note" :class="{ 'is-waiting': step.waiting }">{{ step.note }}</span>
          </div>
          <div v-if="step.error && expandedKeys.has(step.key)" class="xl-step-error">{{ step.error }}</div>
        </div>
      </li>
    </ol>
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
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--gray-100);
}
.xl-steps-title { font-weight: 650; }
.xl-steps-summary { margin-left: auto; color: var(--gray-500); font-size: 10.5px; white-space: nowrap; }

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
  animation: xl-breathe 1.1s ease-in-out infinite;
}
.xl-rag-pulse { background: var(--accent-500); }
.xl-page-pulse { background: var(--brand-500); }
@keyframes xl-breathe { 0%, 100% { opacity: 0.35; transform: scale(0.8); } 50% { opacity: 1; transform: scale(1.1); } }

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
  animation: xl-audit-breathe 1.8s ease-in-out infinite;
}
@keyframes xl-audit-breathe { 0%, 100% { opacity: 0.8; } 50% { opacity: 1; } }
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
@media (prefers-reduced-motion: reduce) {
  .xl-audit-phase.is-latest:last-child { animation: none; }
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

.xl-step-note {
  margin-left: auto;
  flex: none;
  color: var(--gray-500);
  font-size: 10.5px;
  white-space: nowrap;
}
.xl-step.is-running .xl-step-note { color: var(--brand-600); animation: xl-note-breathe 1.6s ease-in-out infinite; }
.xl-step-note.is-waiting { color: var(--sev-medium); font-weight: 600; }
@keyframes xl-note-breathe { 0%, 100% { opacity: 0.55; } 50% { opacity: 1; } }

.xl-step-error {
  margin-top: 3px;
  color: var(--color-danger);
  font-size: 10.5px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

@media (prefers-reduced-motion: reduce) {
  .xl-step-spinner, .xl-rag-pulse, .xl-page-pulse { animation: none; }
  .xl-step.is-running .xl-step-note { animation: none; }
}
</style>
