<script setup lang="ts">
defineProps<{ current: 'user' | 'admin'; adminUnread?: number }>()
defineEmits<{ select: [surface: 'user' | 'admin'] }>()
</script>

<template>
  <div class="surface-switch" role="group" aria-label="助手会话类型">
    <button type="button" :aria-pressed="current === 'user'" data-surface="user" @click="current !== 'user' && $emit('select', 'user')">代码会话</button>
    <button type="button" :aria-pressed="current === 'admin'" data-surface="admin" @click="current !== 'admin' && $emit('select', 'admin')">
      管理会话<span v-if="adminUnread" class="unread" aria-label="有未读异常"></span>
    </button>
  </div>
</template>

<style scoped>
.surface-switch { display: inline-flex; gap: 2px; padding: 2px; border-radius: 8px; background: var(--gray-100, #f3f4f6); }
.surface-switch button { position: relative; min-height: 32px; padding: 4px 8px; border: 0; border-radius: 6px; background: transparent; color: var(--gray-600, #596173); cursor: pointer; white-space: nowrap; }
.surface-switch button[aria-pressed="true"] { background: white; color: var(--brand-600, #514de5); }
.surface-switch button:focus-visible { outline: 2px solid var(--brand-500, #635bff); outline-offset: 2px; }
.unread { position: absolute; top: 1px; right: 1px; width: 7px; height: 7px; border-radius: 50%; background: var(--agent-danger, #e64a62); }
</style>
