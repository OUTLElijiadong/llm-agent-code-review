<script setup lang="ts">
import { computed, ref } from 'vue'
import { ArrowRight, Loading, WarningFilled } from '@element-plus/icons-vue'

import type { ResponseApprovalDecision, ResponseApprovalRequiredEvent } from '@/types/responses'
import { formatResponseValue } from '@/utils/responsesTimeline'
import { toolDisplayInfo } from '@/utils/toolDisplay'

type ApprovalStatus = 'pending' | 'submitting' | 'approved' | 'rejected'

const props = defineProps<{
  approval: ResponseApprovalRequiredEvent & { status: ApprovalStatus }
  loading: boolean
}>()

const emit = defineEmits<{ decide: [decision: ResponseApprovalDecision] }>()

const argumentsText = computed(() => formatResponseValue(props.approval.arguments))
const previewText = computed(() => formatResponseValue(props.approval.preview))
/** 标题:操作名优先,函数名兜底时翻译成通俗中文,不外露原始 tool_name。 */
const titleText = computed(() => props.approval.operation || toolDisplayInfo(props.approval.tool_name).label)
/** 提交中(本卡片 submitting 或整局面板 loading):批准/拒绝按钮禁用并转圈,防重复点击。 */
const submitting = computed(() => props.approval.status === 'submitting' || props.loading)
const canApprove = computed(() => !submitting.value)
const resultLabel = computed(() => {
  if (props.approval.status === 'submitting') return '处理中'
  return props.approval.status === 'approved' ? '已批准' : '已拒绝'
})

/** 「调用参数」默认折叠为技术细节,操作预览保持可见。 */
const detailOpen = ref(false)
</script>

<template>
  <section class="response-approval-card approval-card response-approval" :class="{ 'is-danger': approval.danger }">
    <header>
      <el-icon><WarningFilled /></el-icon>
      <span>{{ titleText }}</span>
    </header>
    <p v-if="approval.impact" class="response-approval-impact">{{ approval.impact }}</p>
    <div v-if="previewText" class="response-approval-section">
      <b>操作预览</b>
      <pre class="response-approval-preview">{{ previewText }}</pre>
    </div>
    <div v-if="argumentsText" class="response-approval-section">
      <button
        class="response-approval-detail-toggle"
        type="button"
        :aria-expanded="detailOpen"
        @click="detailOpen = !detailOpen"
      >
        <el-icon class="response-approval-detail-caret" :class="{ 'is-open': detailOpen }"><ArrowRight /></el-icon>
        调用参数（技术细节）
      </button>
      <pre v-if="detailOpen" class="response-approval-arguments">{{ argumentsText }}</pre>
    </div>
    <div v-if="approval.status === 'pending'" class="response-control-actions card-actions">
      <button
        class="response-approve primary-action"
        type="button"
        :disabled="!canApprove"
        @click="emit('decide', { action: 'approve', confirmation: approval.danger ? '确认执行' : '' })"
      >
        <el-icon v-if="submitting" class="is-spinning" aria-hidden="true"><Loading /></el-icon>
        <el-icon v-else-if="approval.danger" class="response-approve-icon"><WarningFilled /></el-icon>
        {{ submitting ? '处理中…' : approval.danger ? '确认执行' : '批准' }}
      </button>
      <button class="response-reject secondary-action" type="button" :disabled="submitting" @click="emit('decide', { action: 'reject' })">拒绝</button>
    </div>
    <div v-else class="response-control-result card-result" :class="approval.status">{{ resultLabel }}</div>
  </section>
</template>

<style scoped>
.response-approval-card { box-sizing: border-box; width: 100%; min-width: 0; margin-top: var(--sp-2); padding: 11px var(--sp-3); border: 1px solid var(--color-border-base); border-radius: var(--r-md); background: var(--color-bg-card); color: var(--color-text-primary); font-size: var(--fs-xs); line-height: 1.5; }
.response-approval-card.is-danger { border-color: var(--color-danger); }
.response-approval-card > header { display: flex; align-items: center; gap: 6px; font-size: var(--fs-sm); font-weight: 650; }
.is-danger > header { color: var(--color-danger); }
.response-approval-impact { margin: 7px 0 0; color: var(--color-text-regular); }
.response-approval-section { margin-top: 9px; }
.response-approval-section b { display: block; margin-bottom: var(--sp-1); color: var(--color-text-regular); font-size: var(--fs-xs); }
.response-approval-section pre { max-height: 150px; margin: 0; padding: 7px var(--sp-2); overflow: auto; border: 1px solid var(--color-border-light); border-radius: var(--r-sm); background: var(--gray-50); color: var(--gray-700); font: var(--fs-xs)/1.45 var(--font-mono); white-space: pre-wrap; overflow-wrap: anywhere; }
.response-approval-detail-toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  border: 0;
  background: transparent;
  padding: 0;
  color: var(--color-text-secondary);
  font-size: var(--fs-xs);
  cursor: pointer;
  border-radius: var(--r-sm);
}
.response-approval-detail-toggle:hover { color: var(--brand-600); }
.response-approval-detail-toggle:focus-visible { outline: 2px solid var(--brand-300); outline-offset: 2px; }
.response-approval-detail-caret { font-size: var(--fs-xs); transition: transform var(--transition-fast); }
.response-approval-detail-caret.is-open { transform: rotate(90deg); }
.response-approval-detail-toggle + pre { margin-top: var(--sp-1); }
.response-control-actions { display: flex; flex-wrap: wrap; gap: var(--sp-2); margin-top: var(--sp-3); }
.response-control-actions button {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  min-height: 32px;
  padding: 0 var(--sp-3);
  border-radius: var(--r-sm);
  cursor: pointer;
}
.response-approve { border: 1px solid var(--brand-600); color: #fff; background: var(--brand-600); }
.response-approve:hover:not(:disabled) { border-color: var(--brand-700); background: var(--brand-700); }
/* 高危操作:醒目的红色确认按钮,单击即提交 */
.is-danger .response-approve {
  border-color: var(--color-danger);
  background: var(--color-danger);
  font-weight: 650;
  box-shadow: 0 2px 8px -2px rgba(220, 73, 97, 0.45);
}
.is-danger .response-approve:hover:not(:disabled) { border-color: var(--sev-severe); background: var(--sev-severe); }
.response-reject { border: 1px solid var(--color-border-base); color: var(--color-text-regular); background: var(--color-bg-card); }
button:disabled { opacity: .5; cursor: not-allowed; }
.response-control-result { margin-top: 9px; padding: 6px var(--sp-2); border-radius: var(--r-sm); color: var(--color-success); background: var(--color-success-light); }
.response-control-result.rejected { color: var(--color-danger); background: var(--color-danger-light); }

/* 提交中旋转指示(复用 Element Plus 约定类名) */
.is-spinning { animation: response-spin 1s linear infinite; }
@keyframes response-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .is-spinning { animation: none; } }
</style>
