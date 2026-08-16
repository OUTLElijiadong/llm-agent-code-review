<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { findPageGuideTip, type PageGuideTip } from '@/utils/pageGuideTips'

const props = defineProps<{ surface: 'user' | 'admin' }>()

const route = useRoute()
const tip = ref<PageGuideTip | null>(null)
let hideTimer: number | undefined
const seenKey = `prism-page-guide:${props.surface}`

function openCopilot(): void {
  if (!tip.value) return
  const eventName = props.surface === 'admin' ? 'prism:open-admin-copilot' : 'prism:open-agent-chat'
  window.dispatchEvent(new CustomEvent(eventName, { detail: { prefill: tip.value.prompt } }))
  dismiss()
}

function dismiss(): void {
  tip.value = null
  if (hideTimer !== undefined) window.clearTimeout(hideTimer)
}

function evaluate(): void {
  if (hideTimer !== undefined) {
    window.clearTimeout(hideTimer)
    hideTimer = undefined
  }
  const path = route.fullPath.split('?')[0]
  const matched = findPageGuideTip(props.surface, path)
  const seen = (window.sessionStorage.getItem(seenKey) ?? '').split(',')
  // 已看过或无建议都必须清掉旧 tip,否则上一个页面的引导会残留在当前页且不再自动消失。
  if (!matched || seen.includes(path)) {
    tip.value = null
    return
  }
  window.sessionStorage.setItem(seenKey, [...seen, path].slice(-20).join(','))
  tip.value = matched
  hideTimer = window.setTimeout(() => { tip.value = null }, 12_000)
}

watch(() => route.fullPath, evaluate, { immediate: true })
onBeforeUnmount(() => { if (hideTimer !== undefined) window.clearTimeout(hideTimer) })
</script>

<template>
  <Transition name="guide-pop">
    <div v-if="tip" class="proactive-guide" role="status" aria-live="polite">
      <span class="guide-mascot" aria-hidden="true">🦾</span>
      <div class="guide-body">
        <strong class="guide-title">{{ tip.title }} · 下一步建议</strong>
        <span class="guide-hint">{{ tip.hint }}</span>
      </div>
      <button class="guide-act" type="button" @click="openCopilot">让小菱引导</button>
      <button class="guide-close" type="button" aria-label="关闭引导" @click="dismiss">×</button>
    </div>
  </Transition>
</template>

<style scoped>
.proactive-guide {
  position: fixed;
  right: 24px;
  bottom: 96px;
  z-index: 2980;
  display: flex;
  align-items: center;
  gap: 10px;
  max-width: 420px;
  padding: 12px 14px;
  border: 1px solid rgba(91, 88, 232, 0.22);
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.97);
  box-shadow: 0 14px 36px rgba(31, 35, 41, 0.16);
}

.guide-mascot {
  font-size: 22px;
  line-height: 1;
  animation: guide-float 1.6s ease-in-out infinite;
}

.guide-body {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.guide-title {
  font-size: 13px;
  color: #1f2329;
}

.guide-hint {
  font-size: 12px;
  line-height: 1.45;
  color: #6b7078;
}

.guide-act {
  flex-shrink: 0;
  padding: 7px 12px;
  border: 1px solid var(--brand-500, #5b58e8);
  border-radius: 9px;
  background: var(--brand-500, #5b58e8);
  color: #fff;
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
}

.guide-act:hover { background: var(--brand-600, #4a47d1); }

.guide-close {
  flex-shrink: 0;
  width: 22px;
  height: 22px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: #9aa1aa;
  font-size: 14px;
  cursor: pointer;
}

.guide-close:hover { background: #f1f2f5; color: #4b5058; }

@keyframes guide-float {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-3px); }
}

.guide-pop-enter-active,
.guide-pop-leave-active { transition: opacity 0.2s ease, transform 0.2s ease; }
.guide-pop-enter-from,
.guide-pop-leave-to { opacity: 0; transform: translateY(8px); }

@media (prefers-reduced-motion: reduce) {
  .guide-mascot { animation: none; }
}
</style>
