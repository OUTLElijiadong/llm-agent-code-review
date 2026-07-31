<script setup lang="ts">
import { computed } from 'vue'
import { CircleCheck, Loading, WarningFilled } from '@element-plus/icons-vue'

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

function statusLabel(status: ResponseToolCallStatus): string {
  return STATUS_LABELS[status]
}

function argumentsText(call: ResponseToolCall): string {
  return formatResponseValue(call.argumentsText)
}
</script>

<template>
  <section v-if="visibleCalls.length" class="response-tool-timeline" aria-label="Agent 工具调用过程">
    <header>Agent 调用链</header>
    <ol>
      <li v-for="call in visibleCalls" :key="call.key" class="response-tool-call" :class="`is-${call.status}`">
        <div class="response-tool-call-head">
          <el-icon class="response-tool-state-icon">
            <CircleCheck v-if="call.status === 'completed'" />
            <WarningFilled v-else-if="call.status === 'failed' || call.status === 'rejected'" />
            <Loading v-else />
          </el-icon>
          <div class="response-tool-identity">
            <code>{{ call.name }}</code>
            <small v-if="call.agentCode">{{ call.agentCode }}</small>
          </div>
          <span>{{ statusLabel(call.status) }}</span>
        </div>
        <pre v-if="argumentsText(call)" class="response-tool-arguments">{{ argumentsText(call) }}</pre>
        <pre v-if="call.resultPreview" class="response-tool-result">{{ call.resultPreview }}</pre>
        <p v-if="call.error" class="response-tool-error">{{ call.error }}</p>
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
.response-tool-timeline > header { padding: 8px 10px; border-bottom: 1px solid #edf0f2; font-weight: 650; }
.response-tool-timeline ol { display: grid; gap: 0; margin: 0; padding: 0; list-style: none; }
.response-tool-call { min-width: 0; padding: 9px 10px; border-left: 3px solid #3978d6; }
.response-tool-call + .response-tool-call { border-top: 1px solid #edf0f2; }
.response-tool-call.is-completed { border-left-color: #2b8a57; }
.response-tool-call.is-failed,
.response-tool-call.is-rejected { border-left-color: #c43d36; }
.response-tool-call.is-waiting_approval,
.response-tool-call.is-waiting_input { border-left-color: #c16b20; }
.response-tool-call-head { display: grid; grid-template-columns: 16px minmax(0, 1fr) auto; align-items: center; gap: 6px; }
.response-tool-identity { display: flex; align-items: baseline; flex-wrap: wrap; gap: 3px 7px; min-width: 0; }
.response-tool-call-head code { min-width: 0; overflow-wrap: anywhere; color: #1756a9; font-size: 11px; }
.response-tool-identity small { color: #7a838f; font-size: 9.5px; overflow-wrap: anywhere; }
.response-tool-call-head span { color: #707781; font-size: 10px; }
.response-tool-state-icon { color: #3978d6; }
.is-streaming .response-tool-state-icon,
.is-running .response-tool-state-icon { animation: response-tool-spin 1s linear infinite; }
.is-completed .response-tool-state-icon { color: #2b8a57; }
.is-failed .response-tool-state-icon,
.is-rejected .response-tool-state-icon { color: #c43d36; }
.response-tool-arguments,
.response-tool-result { max-height: 132px; margin: 7px 0 0 22px; padding: 7px 8px; overflow: auto; border-radius: 5px; background: #f6f7f9; color: #3f4650; font: 10.5px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
.response-tool-result { background: #f1f8f4; }
.response-tool-error { margin: 7px 0 0 22px; color: #b42318; overflow-wrap: anywhere; }
@keyframes response-tool-spin { to { transform: rotate(360deg); } }
</style>
