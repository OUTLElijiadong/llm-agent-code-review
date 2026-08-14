<script setup lang="ts">
import { computed, reactive } from 'vue'
import { ArrowRight, CircleCheck, Loading, WarningFilled } from '@element-plus/icons-vue'

import { toolDisplayInfo } from '@/utils/toolDisplay'
import type { ResponseToolCall, ResponseToolCallStatus } from '@/utils/responsesTimeline'

const props = defineProps<{ calls: ResponseToolCall[] }>()

const STATUS_LABELS: Record<ResponseToolCallStatus, string> = {
  streaming: '接收参数',
  queued: '已排队',
  delivered: '已送达',
  acknowledged: '已确认',
  processing: '处理中',
  running: '执行中',
  waiting_approval: '等待批准',
  waiting_input: '等待输入',
  completed: '已完成',
  failed: '失败',
  rejected: '已拒绝',
}

/** 展开详情只展示通俗文字:优先失败原因,否则给出状态说明。 */
const STATUS_NOTES: Record<ResponseToolCallStatus, string> = {
  streaming: '小菱正在接收执行参数…',
  queued: '任务已排队,等待小菱处理…',
  delivered: '任务已送达,等待小菱确认…',
  acknowledged: '小菱已确认收到任务…',
  processing: '小菱正在处理…',
  running: '小菱正在执行…',
  waiting_approval: '小菱正在等待你的批准…',
  waiting_input: '小菱正在等待你的输入…',
  completed: '此操作已完成',
  failed: '操作失败',
  rejected: '操作已被拒绝',
}

/** Mesh 原始消息名在 toolDisplay 中没有映射,单独翻译。 */
const MESH_SEND_MESSAGE = 'send_message'
const MESH_RECEIVE_MESSAGE = 'receive_message'

/** 单条调用的通俗展示文本:subject 优先,运行/处理中显示进行时短语。 */
function displayText(call: ResponseToolCall): string {
  const subject = call.subject?.trim()
  if (subject) return subject
  if (call.name === MESH_SEND_MESSAGE) return '派发任务'
  if (call.name === MESH_RECEIVE_MESSAGE) return '收到子Agent结果'
  const info = toolDisplayInfo(call.name)
  if (call.status === 'running' || call.status === 'processing') return info.running
  return info.label
}

/** 展开详情:失败原因优先,其余用通俗状态说明。 */
function detailText(call: ResponseToolCall): string {
  const error = call.error?.trim()
  if (error) return error
  return STATUS_NOTES[call.status] ?? '暂无更多说明'
}

const visibleCalls = computed(() => props.calls.filter((call) => call.name || call.argumentsText))

/** 每条调用的展开状态;默认全部折叠,用户点击后展开,保持用户选择。 */
const expandedKeys = reactive(new Set<string>())

function toggle(call: ResponseToolCall): void {
  if (expandedKeys.has(call.key)) expandedKeys.delete(call.key)
  else expandedKeys.add(call.key)
}

function statusLabel(status: ResponseToolCallStatus): string {
  return STATUS_LABELS[status]
}

/** 只要还有未展开的调用就显示「展开全部」,否则显示「收起全部」。 */
const anyCollapsed = computed(() => visibleCalls.value.some((call) => !expandedKeys.has(call.key)))

function toggleAll(): void {
  if (anyCollapsed.value) {
    for (const call of visibleCalls.value) expandedKeys.add(call.key)
  } else {
    expandedKeys.clear()
  }
}
</script>

<template>
  <section v-if="visibleCalls.length" class="response-tool-timeline" aria-label="小菱操作记录">
    <header>
      <span>小菱操作记录 · {{ visibleCalls.length }} 步</span>
      <button
        v-if="visibleCalls.length > 1"
        class="response-tool-collapse-all"
        type="button"
        :aria-label="anyCollapsed ? '展开全部调用' : '收起全部调用'"
        @click="toggleAll"
      >
        {{ anyCollapsed ? '展开全部' : '收起全部' }}
      </button>
    </header>
    <ol>
      <li v-for="call in visibleCalls" :key="call.key" class="response-tool-call" :class="`is-${call.status}`">
        <div
          class="response-tool-call-head"
          role="button"
          tabindex="0"
          :aria-expanded="expandedKeys.has(call.key)"
          :aria-label="`${displayText(call)} ${statusLabel(call.status)}`"
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
            <strong class="response-tool-action">{{ displayText(call) }}</strong>
          </div>
          <span class="response-tool-status">{{ statusLabel(call.status) }}</span>
          <el-icon class="response-tool-caret" :class="{ 'is-open': expandedKeys.has(call.key) }">
            <ArrowRight />
          </el-icon>
        </div>
        <div v-if="expandedKeys.has(call.key)" class="response-tool-detail">
          <p class="response-tool-note">{{ detailText(call) }}</p>
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
.response-tool-timeline > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid #edf0f2;
  font-weight: 650;
}
.response-tool-collapse-all {
  flex: none;
  border: 0;
  background: transparent;
  color: #3978d6;
  font-size: 12px;
  cursor: pointer;
  padding: 2px 4px;
  border-radius: 4px;
}
.response-tool-collapse-all:hover { background: #f0f5ff; }
.response-tool-timeline ol { display: grid; gap: 0; margin: 0; padding: 0; list-style: none; }
.response-tool-call { min-width: 0; padding: 9px 10px; border-left: 3px solid #3978d6; }
.response-tool-call + .response-tool-call { border-top: 1px solid #edf0f2; }
.response-tool-call.is-completed { border-left-color: #2b8a57; }
.response-tool-call.is-failed,
.response-tool-call.is-rejected { border-left-color: #c43d36; }
.response-tool-call.is-waiting_approval,
.response-tool-call.is-waiting_input { border-left-color: #c16b20; }
.response-tool-call.is-queued { border-left-color: #8a93a0; }
.response-tool-call.is-delivered,
.response-tool-call.is-acknowledged { border-left-color: #3978d6; }
.response-tool-call.is-processing { border-left-color: #c16b20; }
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
.response-tool-action { min-width: 0; color: #252a31; font-size: 12px; font-weight: 650; overflow-wrap: anywhere; }
.response-tool-status { color: #707781; font-size: 10px; white-space: nowrap; }
.response-tool-state-icon { color: #3978d6; }
.is-streaming .response-tool-state-icon,
.is-processing .response-tool-state-icon,
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
.response-tool-note { margin: 7px 0 0 22px; color: #59616c; font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }
@keyframes response-tool-spin { to { transform: rotate(360deg); } }
</style>
