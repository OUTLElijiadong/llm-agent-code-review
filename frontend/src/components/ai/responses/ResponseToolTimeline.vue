<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ArrowRight, CircleCheck, Loading, WarningFilled } from '@element-plus/icons-vue'

import type { ResponseToolCall, ResponseToolCallStatus } from '@/utils/responsesTimeline'
import { formatResponseValue } from '@/utils/responsesTimeline'

const props = defineProps<{ calls: ResponseToolCall[] }>()

const STATUS_LABELS: Record<ResponseToolCallStatus, string> = {
  streaming: '接收参数',
  running: '执行中',
  waiting_approval: '等待批准',
  waiting_input: '等待输入',
  completed: '已完成',
  failed: '失败',
  rejected: '已拒绝',
}

const visibleCalls = computed(() => props.calls.filter((call) => call.name || call.argumentsText))

const ACTIVE_STATUSES: ReadonlySet<ResponseToolCallStatus> = new Set([
  'streaming',
  'running',
  'waiting_approval',
  'waiting_input',
])

/** 整块(调用链面板)的展开状态;有进行中调用时默认展开,全部完成后收回折叠。 */
const blockExpanded = ref(false)

/** 每条调用的展开状态;进行中的调用默认展开,完成后保持用户当前选择。 */
const expandedKeys = reactive(new Set<string>())

function isActive(status: ResponseToolCallStatus): boolean {
  return ACTIVE_STATUSES.has(status)
}

const activeCalls = computed(() => visibleCalls.value.filter((call) => isActive(call.status)))
const hasActive = computed(() => activeCalls.value.length > 0)
const doneCount = computed(() => visibleCalls.value.filter((call) => call.status === 'completed').length)
const failedCount = computed(() =>
  visibleCalls.value.filter((call) => call.status === 'failed' || call.status === 'rejected').length,
)

/** 摘要行:进行中显示当前动作,否则显示汇总。 */
const summaryText = computed(() => {
  if (hasActive.value) {
    const current = activeCalls.value[activeCalls.value.length - 1]
    return `正在执行 ${current.name}`
  }
  const parts: string[] = []
  if (doneCount.value) parts.push(`${doneCount.value} 个完成`)
  if (failedCount.value) parts.push(`${failedCount.value} 个失败`)
  return parts.length ? parts.join(' · ') : '已完成'
})

function initExpanded(call: ResponseToolCall): void {
  if (isActive(call.status) && !expandedKeys.has(call.key)) expandedKeys.add(call.key)
}

// 进行中/新增调用:整块保持展开,单条自动展开;全部完成后整块收回折叠(用户手动展开过则尊重)。
watch(
  () => props.calls.map((call) => `${call.key}:${call.status}`),
  (now, prev) => {
    for (const call of props.calls) initExpanded(call)
    const anyActiveNow = props.calls.some((call) => isActive(call.status))
    const anyActiveBefore = (prev ?? []).some((entry) => {
      const status = entry.split(':').pop() as ResponseToolCallStatus | undefined
      return status ? isActive(status) : false
    })
    // 进行中 → 展开整块;由进行中变为全部完成 → 若用户没手动操作过,收回折叠
    if (anyActiveNow) blockExpanded.value = true
    else if (anyActiveBefore && !anyActiveNow && !userToggled.value) blockExpanded.value = false
  },
  { immediate: true },
)

/** 用户是否手动点过整块折叠,避免自动收起打断用户查看。 */
const userToggled = ref(false)

function toggleBlock(): void {
  blockExpanded.value = !blockExpanded.value
  userToggled.value = true
}

function toggle(call: ResponseToolCall): void {
  if (expandedKeys.has(call.key)) expandedKeys.delete(call.key)
  else expandedKeys.add(call.key)
}

function statusLabel(status: ResponseToolCallStatus): string {
  return STATUS_LABELS[status]
}

function argumentsText(call: ResponseToolCall): string {
  return formatResponseValue(call.argumentsText)
}
</script>

<template>
  <section v-if="visibleCalls.length" class="response-tool-timeline" aria-label="Agent 工具调用过程">
    <button
      type="button"
      class="response-tool-summary"
      :class="{ 'is-active': hasActive }"
      :aria-expanded="blockExpanded"
      :aria-label="`Agent 调用链,${visibleCalls.length} 个工具,${summaryText}`"
      @click="toggleBlock"
    >
      <el-icon class="response-tool-summary-state" :class="{ 'is-running': hasActive }">
        <Loading v-if="hasActive" />
        <WarningFilled v-else-if="failedCount > 0" />
        <CircleCheck v-else />
      </el-icon>
      <span class="response-tool-summary-text">
        Agent 调用链 · {{ visibleCalls.length }} 个工具
        <em class="response-tool-summary-live">{{ summaryText }}</em>
      </span>
      <el-icon class="response-tool-caret" :class="{ 'is-open': blockExpanded }">
        <ArrowRight />
      </el-icon>
    </button>

    <ol v-if="blockExpanded">
      <li v-for="call in visibleCalls" :key="call.key" class="response-tool-call" :class="`is-${call.status}`">
        <div
          class="response-tool-call-head"
          role="button"
          tabindex="0"
          :aria-expanded="expandedKeys.has(call.key)"
          :aria-label="`${call.name} ${statusLabel(call.status)}`"
          @click="toggle(call)"
          @keydown.enter="toggle(call)"
          @keydown.space.prevent="toggle(call)"
        >
          <el-icon class="response-tool-state-icon">
            <CircleCheck v-if="call.status === 'completed'" />
            <WarningFilled v-else-if="call.status === 'failed' || call.status === 'rejected'" />
            <Loading v-else />
          </el-icon>
          <div class="response-tool-identity">
            <code>{{ call.name }}</code>
            <small v-if="call.agentCode">{{ call.agentCode }}</small>
          </div>
          <span class="response-tool-status">{{ statusLabel(call.status) }}</span>
          <el-icon class="response-tool-caret" :class="{ 'is-open': expandedKeys.has(call.key) }">
            <ArrowRight />
          </el-icon>
        </div>
        <div v-if="expandedKeys.has(call.key)" class="response-tool-detail">
          <pre v-if="argumentsText(call)" class="response-tool-arguments">{{ argumentsText(call) }}</pre>
          <pre v-if="call.resultPreview" class="response-tool-result">{{ call.resultPreview }}</pre>
          <p v-if="call.error" class="response-tool-error">{{ call.error }}</p>
        </div>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.response-tool-timeline {
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  overflow: hidden;
  border: 1px solid #dfe3e8;
  border-radius: 8px;
  background: #fff;
  color: #1f2329;
  font-size: 12px;
  margin-top: 8px;
}
.response-tool-summary {
  display: grid;
  grid-template-columns: 16px minmax(0, 1fr) 14px;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 8px 10px;
  border: 0;
  background: transparent;
  color: #1f2329;
  font: inherit;
  font-weight: 650;
  text-align: left;
  cursor: pointer;
  user-select: none;
}
.response-tool-summary:hover { background: #f5f7fa; }
.response-tool-summary:focus-visible { outline: 2px solid rgba(57, 120, 214, 0.5); outline-offset: -2px; border-radius: 8px; }
.response-tool-summary-text { min-width: 0; overflow-wrap: anywhere; }
.response-tool-summary-live {
  margin-left: 6px;
  color: #707781;
  font-style: normal;
  font-weight: 500;
  font-size: 10.5px;
}
.response-tool-summary-state { color: #2b8a57; }
.response-tool-summary.is-active .response-tool-summary-state,
.response-tool-summary-state.is-running { color: #3978d6; animation: response-tool-spin 1s linear infinite; }
.response-tool-timeline ol { display: grid; gap: 0; margin: 0; padding: 0; list-style: none; border-top: 1px solid #edf0f2; }
.response-tool-call { min-width: 0; padding: 9px 10px; border-left: 3px solid #3978d6; }
.response-tool-call + .response-tool-call { border-top: 1px solid #edf0f2; }
.response-tool-call.is-completed { border-left-color: #2b8a57; }
.response-tool-call.is-failed,
.response-tool-call.is-rejected { border-left-color: #c43d36; }
.response-tool-call.is-waiting_approval,
.response-tool-call.is-waiting_input { border-left-color: #c16b20; }
.response-tool-call-head {
  display: grid;
  grid-template-columns: 16px minmax(0, 1fr) auto 14px;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  user-select: none;
}
.response-tool-call-head:focus-visible { outline: 2px solid rgba(57, 120, 214, 0.5); outline-offset: 2px; border-radius: 4px; }
.response-tool-identity { display: flex; align-items: baseline; flex-wrap: wrap; gap: 3px 7px; min-width: 0; }
.response-tool-call-head code { min-width: 0; overflow-wrap: anywhere; color: #1756a9; font-size: 11px; }
.response-tool-identity small { color: #7a838f; font-size: 9.5px; overflow-wrap: anywhere; }
.response-tool-status { color: #707781; font-size: 10px; white-space: nowrap; }
.response-tool-state-icon { color: #3978d6; }
.is-streaming .response-tool-state-icon,
.is-running .response-tool-state-icon { animation: response-tool-spin 1s linear infinite; }
.is-completed .response-tool-state-icon { color: #2b8a57; }
.is-failed .response-tool-state-icon,
.is-rejected .response-tool-state-icon { color: #c43d36; }
.response-tool-caret {
  color: #a6aeb8;
  font-size: 11px;
  transition: transform 0.15s ease;
}
.response-tool-caret.is-open { transform: rotate(90deg); }
.response-tool-detail { min-width: 0; }
.response-tool-arguments,
.response-tool-result { max-height: 132px; margin: 7px 0 0 22px; padding: 7px 8px; overflow: auto; border-radius: 5px; background: #f6f7f9; color: #3f4650; font: 10.5px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
.response-tool-result { background: #f1f8f4; }
.response-tool-error { margin: 7px 0 0 22px; color: #b42318; overflow-wrap: anywhere; }
@keyframes response-tool-spin { to { transform: rotate(360deg); } }
</style>
