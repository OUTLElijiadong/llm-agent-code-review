<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'

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

function chooseOption(value: string): void {
  emit('update:answer', value)
  emit('submit', value)
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
          type="button"
          :disabled="loading"
          @click="chooseOption(option.value)"
        >
          <span>{{ option.label }}</span>
          <small v-if="option.description">{{ option.description }}</small>
        </button>
      </div>
      <button v-if="options.length && allowsFreeText" class="response-custom-label" type="button" :disabled="loading" @click="focusCustom">其他（自定义输入）</button>
      <textarea
        v-if="allowsFreeText || !options.length"
        ref="customInput"
        class="response-answer"
        :value="request.answer"
        rows="3"
        :disabled="loading"
        placeholder="输入其他答案"
        @input="emit('update:answer', ($event.target as HTMLTextAreaElement).value)"
      />
      <button
        v-if="allowsFreeText || !options.length"
        class="response-answer-submit"
        type="button"
        :disabled="loading || !request.answer.trim()"
        @click="emit('submit')"
      >提交</button>
    </template>
    <div v-else class="response-control-result">{{ request.status === 'submitting' ? '提交中' : '已提交' }}</div>
  </section>
</template>

<style scoped>
.response-input-card { box-sizing: border-box; width: 100%; min-width: 0; margin-top: 8px; padding: 11px 12px; border: 1px solid #dfe3e8; border-radius: 8px; background: #fff; color: #1f2329; font-size: 12px; }
.response-question { margin: 0 0 9px; color: #3f4650; font-weight: 650; line-height: 1.5; }
.response-input-options { display: grid; gap: 7px; }
.response-input-option { display: grid; gap: 2px; width: 100%; min-height: 38px; padding: 7px 9px; border: 1px solid #cfd8e5; border-radius: 6px; color: #174f94; background: #f5f9ff; text-align: left; cursor: pointer; }
.response-input-option:hover:not(:disabled) { border-color: #3978d6; background: #edf5ff; }
.response-input-option span { overflow-wrap: anywhere; }
.response-input-option small { color: #687383; line-height: 1.4; overflow-wrap: anywhere; }
.response-custom-label { margin: 9px 0 5px; padding: 0; border: 0; color: #526171; background: transparent; font-size: 11px; cursor: pointer; }
.response-answer { box-sizing: border-box; width: 100%; min-height: 66px; resize: vertical; padding: 8px 9px; border: 1px solid #d7dce3; border-radius: 6px; outline: none; font: inherit; }
.response-answer:focus { border-color: #3978d6; box-shadow: 0 0 0 2px rgba(57, 120, 214, .12); }
.response-answer-submit { min-height: 32px; margin-top: 8px; padding: 0 12px; border: 1px solid #1769d2; border-radius: 6px; color: #fff; background: #1769d2; cursor: pointer; }
button:disabled { opacity: .5; cursor: not-allowed; }
.response-control-result { margin-top: 8px; color: #526171; }
</style>
