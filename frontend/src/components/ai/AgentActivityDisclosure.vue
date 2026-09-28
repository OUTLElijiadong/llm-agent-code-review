<script setup lang="ts">
import { provide, ref } from 'vue'

import { messageActivityExpandedKey } from './agentActivityDisclosure'

const props = withDefaults(defineProps<{
  summary: string
  state?: 'running' | 'completed' | 'attention'
}>(), {
  state: 'completed',
})

const expanded = ref(false)
provide(messageActivityExpandedKey, expanded)

function syncExpanded(event: Event): void {
  expanded.value = (event.currentTarget as HTMLDetailsElement).open
}
</script>

<template>
  <details class="agent-message-activity" @toggle="syncExpanded">
    <summary class="agent-message-activity-summary">
      <span class="agent-message-activity-caret" :class="{ 'is-open': expanded }" aria-hidden="true">›</span>
      <span class="agent-message-activity-title">执行过程</span>
      <span class="agent-message-activity-state" :class="`is-${props.state}`">
        {{ props.state === 'running' ? '进行中' : props.state === 'attention' ? '需留意' : '已完成' }}
      </span>
      <span class="agent-message-activity-count" role="status">{{ summary }}</span>
    </summary>
    <div class="agent-message-activity-body"><slot /></div>
  </details>
</template>

<style scoped>
.agent-message-activity {
  min-width: 0;
  margin: 8px 0 10px;
  overflow: hidden;
  border: 1px solid var(--gray-200, #e5e7eb);
  border-radius: 10px;
  background: var(--gray-50, #f9fafb);
  color: var(--gray-700, #374151);
  font-size: 12px;
}

.agent-message-activity-summary {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  min-height: 40px;
  padding: 8px 11px;
  cursor: pointer;
  list-style: none;
}

.agent-message-activity-summary::-webkit-details-marker { display: none; }
.agent-message-activity-summary:focus-visible { outline: 2px solid var(--brand-500, #5b58e8); outline-offset: -2px; }
.agent-message-activity-caret { flex: none; color: var(--gray-500, #6b7280); font-size: 18px; line-height: 14px; transition: transform 150ms ease; }
.agent-message-activity-caret.is-open { transform: rotate(90deg); }
.agent-message-activity-title { flex: none; font-weight: 650; }
.agent-message-activity-state { flex: none; border-radius: 999px; padding: 2px 7px; font-size: 10px; }
.agent-message-activity-state.is-running { background: #e8f5ff; color: #1672b8; }
.agent-message-activity-state.is-completed { background: #eaf7ef; color: #21854a; }
.agent-message-activity-state.is-attention { background: #fff0f0; color: #c33d4d; }
.agent-message-activity-count { min-width: 0; margin-left: auto; color: var(--gray-500, #6b7280); text-align: right; overflow-wrap: anywhere; }
.agent-message-activity-body { min-width: 0; padding: 0 10px 10px; border-top: 1px solid var(--gray-200, #e5e7eb); }

@media (max-width: 420px) {
  .agent-message-activity-summary { flex-wrap: wrap; align-items: center; column-gap: 6px; }
  .agent-message-activity-count { flex: 1 0 100%; margin: 0 0 0 25px; text-align: left; }
}

@media (prefers-reduced-motion: reduce) {
  .agent-message-activity-caret { transition: none; }
}
</style>
