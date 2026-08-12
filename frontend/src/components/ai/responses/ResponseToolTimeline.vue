<script setup lang="ts">
import { computed, reactive } from 'vue'
import { ArrowRight, CircleCheck, Loading, Search, WarningFilled } from '@element-plus/icons-vue'

import type { ResponseToolCall, ResponseToolCallStatus } from '@/utils/responsesTimeline'
import { formatResponseValue } from '@/utils/responsesTimeline'
import { toolDisplayInfo } from '@/utils/toolDisplay'

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

/** 每条调用「技术细节」的展开状态;默认折叠,保持用户选择。 */
const expandedKeys = reactive(new Set<string>())

function toggle(call: ResponseToolCall): void {
  if (expandedKeys.has(call.key)) expandedKeys.delete(call.key)
  else expandedKeys.add(call.key)
}

function statusLabel(status: ResponseToolCallStatus): string {
  return STATUS_LABELS[status]
}

/** 通俗展示名(原始函数名只进 aria-label,不再外露)。 */
function displayLabel(call: ResponseToolCall): string {
  return toolDisplayInfo(call.name).label
}

/** 运行中的 RAG 检索类工具给出「检索中」专属文案。 */
function displayRunning(call: ResponseToolCall): string {
  return toolDisplayInfo(call.name).running
}

function isRagRunning(call: ResponseToolCall): boolean {
  return toolDisplayInfo(call.name).isRag && call.status === 'running'
}

function hasDetail(call: ResponseToolCall): boolean {
  return Boolean(formatResponseValue(call.argumentsText) || call.resultPreview || call.error)
}

/** 只要还有未展开的技术细节就显示「展开全部」,否则显示「收起全部」。 */
const anyCollapsed = computed(() => visibleCalls.value.some((call) => hasDetail(call) && !expandedKeys.has(call.key)))

function toggleAll(): void {
  if (anyCollapsed.value) {
    for (const call of visibleCalls.value) expandedKeys.add(call.key)
  } else {
    expandedKeys.clear()
  }
}

function argumentsText(call: ResponseToolCall): string {
  return formatResponseValue(call.argumentsText)
}
</script>

<template>
  <section v-if="visibleCalls.length" class="response-tool-timeline" aria-label="小菱的操作步骤">
    <header>
      <span>小菱的操作步骤</span>
      <button
        v-if="visibleCalls.length > 1"
        class="response-tool-collapse-all"
        type="button"
        :aria-label="anyCollapsed ? '展开全部技术细节' : '收起全部技术细节'"
        @click="toggleAll"
      >
        {{ anyCollapsed ? '展开全部' : '收起全部' }}
      </button>
    </header>
    <ol>
      <li
        v-for="call in visibleCalls"
        :key="call.key"
        class="response-tool-call"
        :class="[`is-${call.status}`, { 'is-rag': isRagRunning(call) }]"
      >
        <div
          class="response-tool-call-head"
          role="button"
          tabindex="0"
          :aria-expanded="hasDetail(call) ? expandedKeys.has(call.key) : undefined"
          :aria-label="`${displayLabel(call)} ${statusLabel(call.status)}${call.agentCode ? `（${call.agentCode}）` : ''}`"
          @click="hasDetail(call) && toggle(call)"
          @keydown.enter="hasDetail(call) && toggle(call)"
          @keydown.space.prevent="hasDetail(call) && toggle(call)"
        >
          <el-icon class="response-tool-state-icon">
            <CircleCheck v-if="call.status === 'completed'" />
            <WarningFilled v-else-if="call.status === 'failed' || call.status === 'rejected'" />
            <Search v-else-if="isRagRunning(call)" />
            <Loading v-else />
          </el-icon>
          <div class="response-tool-identity">
            <span class="response-tool-label">{{ isRagRunning(call) ? displayRunning(call) : displayLabel(call) }}</span>
          </div>
          <span class="response-tool-status">{{ statusLabel(call.status) }}</span>
          <el-icon v-if="hasDetail(call)" class="response-tool-caret" :class="{ 'is-open': expandedKeys.has(call.key) }">
            <ArrowRight />
          </el-icon>
        </div>
        <div v-if="hasDetail(call) && expandedKeys.has(call.key)" class="response-tool-detail">
          <p class="response-tool-detail-caption">技术细节 · {{ call.name }}</p>
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
  border: 1px solid var(--color-border-base);
  border-radius: var(--r-md);
  background: var(--color-bg-card);
  color: var(--color-text-primary);
  font-size: var(--fs-xs);
  margin-top: var(--sp-2);
}
.response-tool-timeline > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-bottom: 1px solid var(--color-border-light);
  font-weight: 650;
}
.response-tool-collapse-all {
  flex: none;
  border: 0;
  background: transparent;
  color: var(--brand-600);
  font-size: var(--fs-xs);
  cursor: pointer;
  padding: 2px var(--sp-1);
  border-radius: var(--r-sm);
}
.response-tool-collapse-all:hover { background: var(--brand-50); }
.response-tool-timeline ol { display: grid; gap: 0; margin: 0; padding: 0; list-style: none; }
.response-tool-call { min-width: 0; padding: 9px var(--sp-3); border-left: 3px solid var(--color-info); }
.response-tool-call + .response-tool-call { border-top: 1px solid var(--color-border-light); }
.response-tool-call.is-completed { border-left-color: var(--color-success); }
.response-tool-call.is-failed,
.response-tool-call.is-rejected { border-left-color: var(--color-danger); }
.response-tool-call.is-waiting_approval,
.response-tool-call.is-waiting_input { border-left-color: var(--color-warning); }
/* 运行中条目:轻微呼吸脉冲 */
.response-tool-call.is-streaming,
.response-tool-call.is-running {
  animation: response-tool-pulse 1.8s ease-in-out infinite;
}
/* RAG 检索中:品牌紫 → 折射青渐变左边条 */
.response-tool-call.is-rag {
  border-left: 0;
  position: relative;
}
.response-tool-call.is-rag::before {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 3px;
  background: linear-gradient(180deg, var(--brand-400), var(--accent-400));
}
.response-tool-call-head {
  display: grid;
  grid-template-columns: 16px minmax(0, 1fr) auto 14px;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  user-select: none;
}
.response-tool-call-head:focus-visible { outline: 2px solid var(--brand-300); outline-offset: 2px; border-radius: var(--r-sm); }
.response-tool-identity { display: flex; align-items: baseline; flex-wrap: wrap; gap: 3px 7px; min-width: 0; }
.response-tool-label { min-width: 0; overflow-wrap: anywhere; color: var(--color-text-primary); font-weight: 550; }
.response-tool-status { color: var(--color-text-secondary); font-size: var(--fs-xs); white-space: nowrap; }
.response-tool-state-icon { color: var(--color-info); }
.is-streaming .response-tool-state-icon,
.is-running .response-tool-state-icon { animation: response-tool-spin 1s linear infinite; }
.is-completed .response-tool-state-icon { color: var(--color-success); }
.is-failed .response-tool-state-icon,
.is-rejected .response-tool-state-icon { color: var(--color-danger); }
.is-rag .response-tool-state-icon { color: var(--brand-500); }
.response-tool-caret {
  color: var(--gray-400);
  font-size: var(--fs-xs);
  transition: transform var(--transition-fast);
}
.response-tool-caret.is-open { transform: rotate(90deg); }
.response-tool-detail { min-width: 0; }
.response-tool-detail-caption {
  margin: 7px 0 0 22px;
  color: var(--color-text-placeholder);
  font-size: var(--fs-xs);
}
.response-tool-arguments,
.response-tool-result { max-height: 132px; margin: var(--sp-1) 0 0 22px; padding: 7px var(--sp-2); overflow: auto; border-radius: var(--r-sm); background: var(--gray-50); color: var(--gray-700); font: var(--fs-xs)/1.45 var(--font-mono); white-space: pre-wrap; overflow-wrap: anywhere; }
.response-tool-result { background: var(--color-success-light); }
.response-tool-error { margin: 7px 0 0 22px; color: var(--color-danger); overflow-wrap: anywhere; }
@keyframes response-tool-spin { to { transform: rotate(360deg); } }
@keyframes response-tool-pulse {
  0%, 100% { box-shadow: inset 0 0 0 999px rgba(91, 88, 232, 0); }
  50% { box-shadow: inset 0 0 0 999px rgba(91, 88, 232, 0.045); }
}

@media (prefers-reduced-motion: reduce) {
  .response-tool-call.is-streaming,
  .response-tool-call.is-running { animation: none; }
  .is-streaming .response-tool-state-icon,
  .is-running .response-tool-state-icon { animation: none; }
}
</style>
