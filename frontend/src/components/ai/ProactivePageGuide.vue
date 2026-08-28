<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { isNavigationPathAllowed } from '@/utils/agentNavigation'
import { findPageGuideTip, type PageGuideTip } from '@/utils/pageGuideTips'

const props = defineProps<{ surface: 'user' | 'admin' }>()

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const tip = ref<PageGuideTip | null>(null)
let hideTimer: number | undefined
const seenKey = computed(() => (
  `prism-page-guide:${props.surface}:user-${userStore.profile?.id ?? 'anonymous'}`
))

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
  const seen = (window.sessionStorage.getItem(seenKey.value) ?? '').split(',')
  // 已看过或无建议都必须清掉旧 tip,否则上一个页面的引导会残留在当前页且不再自动消失。
  // 这里必须与菜单、搜索和 Agent 导航共用同一条权限判定,否则入口虽然隐藏,
  // 主动提示仍可能泄露一个用户无法打开的页面。
  const allowed = Boolean(
    matched
    && userStore.hasPermission('agent:chat')
    && isNavigationPathAllowed(router, matched.route, userStore),
  )
  if (!matched || !allowed || seen.includes(path)) {
    tip.value = null
    return
  }
  window.sessionStorage.setItem(seenKey.value, [...seen, path].slice(-20).join(','))
  tip.value = matched
  hideTimer = window.setTimeout(() => { tip.value = null }, 12_000)
}

watch(() => [route.fullPath, seenKey.value], evaluate, { immediate: true })
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
      <button class="guide-act" type="button" aria-label="让小菱继续引导" @click="openCopilot">让小菱引导</button>
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
  padding: 12px 14px 12px 16px;
  border: 1px solid rgba(91, 88, 232, 0.22);
  border-radius: 10px;
  background: var(--surface-glass, rgba(255, 255, 255, 0.92));
  box-shadow: var(--panel-shadow, 0 14px 36px rgba(31, 35, 41, 0.16));
  backdrop-filter: blur(16px) saturate(1.12);
  -webkit-backdrop-filter: blur(16px) saturate(1.12);
  overflow: hidden;
}

.proactive-guide::before {
  content: '';
  position: absolute;
  inset: 0 0 auto;
  height: 2px;
  background: linear-gradient(90deg, var(--brand-400, #8f8cf2), var(--accent-400, #56b7a1), transparent 86%);
  pointer-events: none;
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
  min-height: 40px;
  padding: 7px 12px;
  border: 1px solid var(--brand-500, #5b58e8);
  border-radius: 8px;
  background: var(--brand-500, #5b58e8);
  color: #fff;
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
}

.guide-act:hover { background: var(--brand-600, #4a47d1); }

.guide-close {
  flex-shrink: 0;
  width: 40px;
  height: 40px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: #9aa1aa;
  font-size: 14px;
  cursor: pointer;
}

.guide-close:hover { background: #f1f2f5; color: #4b5058; }

.guide-act:focus-visible,
.guide-close:focus-visible {
  outline: 3px solid rgba(91, 88, 232, 0.28);
  outline-offset: 2px;
}

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

@media (max-width: 560px) {
  .proactive-guide {
    right: 12px;
    bottom: 84px;
    left: 12px;
    max-width: none;
    padding-left: 12px;
  }

  .guide-body { min-width: 0; }
  .guide-act { margin-left: 32px; }
}
</style>
