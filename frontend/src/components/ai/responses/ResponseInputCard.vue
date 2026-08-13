<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { Loading } from '@element-plus/icons-vue'

import type { ResponseInputRequiredEvent } from '@/types/responses'
import {
  responseAllowsFreeText,
  responseInputOptions,
  responseInputQuestion,
} from '@/utils/responsesTimeline'

type InputStatus = 'pending' | 'submitting' | 'answered'

const props = defineProps<{
  request: ResponseInputRequiredEvent & { answer: string; status: InputStatus }
  loading: boolean
}>()

const emit = defineEmits<{
  'update:answer': [answer: string]
  submit: [answer?: string]
}>()

const customInput = ref<HTMLTextAreaElement | null>(null)
const question = computed(() => responseInputQuestion(props.request))
const options = computed(() => responseInputOptions(props.request))
const allowsFreeText = computed(() => responseAllowsFreeText(props.request))
/** 提交中(本卡片 submitting 或整局面板 loading):按钮转圈并禁用,防重复点击。 */
const submitting = computed(() => props.request.status === 'submitting' || props.loading)
/** 本地点选记录:只有明确点过的选项才高亮,避免自由文本恰好等于选项值时被误判选中。 */
const pickedOption = ref('')
/** 选择了候选项:点击选项只高亮选中态,需再点「提交」才发送,期间可改选。 */
const pickedValue = computed(() => (
  props.request.answer && props.request.answer === pickedOption.value
    && options.value.some((option) => option.value === pickedOption.value)
    ? pickedOption.value
    : ''
))
const canSubmit = computed(() => (
  props.request.status === 'pending' && !submitting.value && props.request.answer.trim().length > 0
))

function chooseOption(value: string): void {
  if (submitting.value) return
  // 再点一次已选中的选项可取消选择,回到自由输入
  const next = pickedValue.value === value ? '' : value
  pickedOption.value = next
  emit('update:answer', next)
}

/** 自由文本输入:内容偏离已选选项时取消选项高亮,保证可改选/改写。 */
function onCustomInput(event: Event): void {
  emit('update:answer', (event.target as HTMLTextAreaElement).value)
}

function submitAnswer(): void {
  if (!canSubmit.value) return
  emit('submit')
}

function focusCustom(): void {
  void nextTick(() => customInput.value?.focus())
}
</script>

<template>
  <section class="response-input-card input-card">
    <p class="response-question">{{ question }}</p>
    <template v-if="request.status === 'pending'">
      <div v-if="options.length" class="response-input-options" role="list" aria-label="候选答案">
        <button
          v-for="option in options"
          :key="option.value"
          class="response-input-option"
          :class="{ 'is-selected': pickedValue === option.value }"
          type="button"
          :disabled="submitting"
          :aria-pressed="pickedValue === option.value"
          @click="chooseOption(option.value)"
        >
          <span>{{ option.label }}</span>
          <small v-if="option.description">{{ option.description }}</small>
        </button>
      </div>
      <button v-if="options.length && allowsFreeText" class="response-custom-label" type="button" :disabled="submitting" @click="focusCustom">其他（自定义输入）</button>
      <textarea
        v-if="allowsFreeText || !options.length"
        ref="customInput"
        class="response-answer"
        :value="request.answer"
        rows="3"
        :disabled="submitting"
        placeholder="输入其他答案"
        @input="onCustomInput"
      />
      <button
        class="response-answer-submit"
        type="button"
        :disabled="!canSubmit"
        @click="submitAnswer"
      >
        <el-icon v-if="submitting" class="is-spinning" aria-hidden="true"><Loading /></el-icon>
        {{ submitting ? '提交中…' : '提交' }}
      </button>
    </template>
    <div v-else class="response-control-result">
      <el-icon v-if="request.status === 'submitting'" class="is-spinning" aria-hidden="true"><Loading /></el-icon>
      {{ request.status === 'submitting' ? '提交中' : '已提交' }}
    </div>
  </section>
</template>

<style scoped>
.response-input-card { box-sizing: border-box; width: 100%; min-width: 0; margin-top: var(--sp-2); padding: 11px var(--sp-3); border: 1px solid var(--color-border-base); border-radius: var(--r-md); background: var(--color-bg-card); color: var(--color-text-primary); font-size: var(--fs-xs); }
.response-question { margin: 0 0 9px; color: var(--color-text-regular); font-weight: 650; line-height: 1.5; }
.response-input-options { display: grid; gap: 7px; }
.response-input-option { display: grid; gap: 2px; width: 100%; min-height: 38px; padding: 7px 9px; border: 1px solid var(--gray-300); border-radius: 6px; color: var(--brand-700); background: var(--brand-50); text-align: left; cursor: pointer; transition: border-color var(--transition-fast), background var(--transition-fast); }
.response-input-option:hover:not(:disabled) { border-color: var(--brand-400); background: var(--brand-100); }
.response-input-option.is-selected { border-color: var(--brand-500); background: var(--brand-100); box-shadow: 0 0 0 1px var(--brand-500) inset; font-weight: 600; }
.response-input-option span { overflow-wrap: anywhere; }
.response-input-option small { color: var(--gray-500); line-height: 1.4; overflow-wrap: anywhere; }
.response-custom-label { margin: 9px 0 5px; padding: 0; border: 0; color: var(--color-text-secondary); background: transparent; font-size: 11px; cursor: pointer; }
.response-custom-label:hover:not(:disabled) { color: var(--brand-600); }
.response-answer { box-sizing: border-box; width: 100%; min-height: 66px; resize: vertical; padding: 8px 9px; border: 1px solid var(--color-border-base); border-radius: 6px; outline: none; font: inherit; }
.response-answer:focus { border-color: var(--brand-400); box-shadow: 0 0 0 2px rgba(91, 88, 232, 0.12); }
.response-answer-submit { display: inline-flex; align-items: center; justify-content: center; gap: 5px; min-height: 32px; margin-top: 8px; padding: 0 12px; border: 1px solid var(--brand-500); border-radius: 6px; color: #fff; background: var(--brand-500); cursor: pointer; }
.response-answer-submit:hover:not(:disabled) { border-color: var(--brand-600); background: var(--brand-600); }
button:disabled { opacity: .5; cursor: not-allowed; }
.response-control-result { display: flex; align-items: center; gap: 5px; margin-top: 8px; color: var(--color-text-secondary); }

/* 提交中旋转指示(复用 Element Plus 约定类名) */
.is-spinning { animation: response-spin 1s linear infinite; }
@keyframes response-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .is-spinning { animation: none; } }
</style>
