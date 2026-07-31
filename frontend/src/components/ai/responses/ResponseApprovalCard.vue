<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { WarningFilled } from '@element-plus/icons-vue'

import type { ResponseApprovalRequiredEvent } from '@/types/responses'
import { formatResponseValue } from '@/utils/responsesTimeline'

type ApprovalStatus = 'pending' | 'submitting' | 'approved' | 'rejected'

const props = defineProps<{
  approval: ResponseApprovalRequiredEvent & { status: ApprovalStatus }
  loading: boolean
}>()

const emit = defineEmits<{ decide: [action: 'approve' | 'reject'] }>()

const dangerConfirmation = ref('')
const argumentsText = computed(() => formatResponseValue(props.approval.arguments))
const previewText = computed(() => formatResponseValue(props.approval.preview))
const canApprove = computed(() => !props.loading && (!props.approval.danger || dangerConfirmation.value === '确认执行'))
const resultLabel = computed(() => {
  if (props.approval.status === 'submitting') return '处理中'
  return props.approval.status === 'approved' ? '已批准' : '已拒绝'
})
watch(() => props.approval.call_id, () => { dangerConfirmation.value = '' })
</script>

<template>
  <section class="response-approval-card approval-card response-approval" :class="{ 'is-danger': approval.danger }">
    <header>
      <el-icon><WarningFilled /></el-icon>
      <span>{{ approval.operation || approval.tool_name }}</span>
    </header>
    <p v-if="approval.impact" class="response-approval-impact">{{ approval.impact }}</p>
    <code class="response-tool-name">{{ approval.tool_name }}</code>
    <div v-if="previewText" class="response-approval-section">
      <b>操作预览</b>
      <pre class="response-approval-preview">{{ previewText }}</pre>
    </div>
    <div v-if="argumentsText" class="response-approval-section">
      <b>调用参数</b>
      <pre class="response-approval-arguments">{{ argumentsText }}</pre>
    </div>
    <div v-if="approval.status === 'pending'" class="response-control-actions card-actions">
      <input
        v-if="approval.danger"
        v-model="dangerConfirmation"
        class="danger-input"
        type="text"
        autocomplete="off"
        aria-label="高危操作确认"
        placeholder="输入“确认执行”"
        :disabled="loading"
      >
      <button class="response-approve primary-action" type="button" :disabled="!canApprove" @click="emit('decide', 'approve')">{{ approval.danger ? '执行' : '批准' }}</button>
      <button class="response-reject secondary-action" type="button" :disabled="loading" @click="emit('decide', 'reject')">拒绝</button>
    </div>
    <div v-else class="response-control-result card-result" :class="approval.status">{{ resultLabel }}</div>
  </section>
</template>

<style scoped>
.response-approval-card { box-sizing: border-box; width: 100%; min-width: 0; margin-top: 8px; padding: 11px 12px; border: 1px solid #dfe3e8; border-radius: 8px; background: #fff; color: #1f2329; font-size: 12px; line-height: 1.5; }
.response-approval-card.is-danger { border-color: #d54941; }
.response-approval-card > header { display: flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 650; }
.is-danger > header { color: #b42318; }
.response-approval-impact { margin: 7px 0 0; color: #59616c; }
.response-tool-name { display: block; margin-top: 7px; overflow-wrap: anywhere; color: #1756a9; }
.response-approval-section { margin-top: 9px; }
.response-approval-section b { display: block; margin-bottom: 4px; color: #4e5969; font-size: 11px; }
.response-approval-section pre { max-height: 150px; margin: 0; padding: 7px 8px; overflow: auto; border: 1px solid #edf0f2; border-radius: 5px; background: #f6f7f9; color: #343a43; font: 10.5px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
.response-control-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.danger-input { box-sizing: border-box; flex: 1 1 180px; min-width: 0; height: 32px; padding: 0 9px; border: 1px solid #c9ced6; border-radius: 6px; color: #1f2329; background: #fff; font-size: 12px; letter-spacing: 0; outline: none; }
.danger-input:focus { border-color: #b42318; box-shadow: 0 0 0 2px rgb(180 35 24 / 12%); }
.response-control-actions button { min-height: 32px; padding: 0 12px; border-radius: 6px; cursor: pointer; }
.response-approve { border: 1px solid #1769d2; color: #fff; background: #1769d2; }
.is-danger .response-approve { border-color: #c43d36; background: #c43d36; }
.response-reject { border: 1px solid #dfe3e8; color: #4e5969; background: #fff; }
button:disabled { opacity: .5; cursor: not-allowed; }
.response-control-result { margin-top: 9px; padding: 6px 8px; border-radius: 5px; color: #216e45; background: #eaf6ef; }
.response-control-result.rejected { color: #6b3a37; background: #fff0ef; }
</style>
