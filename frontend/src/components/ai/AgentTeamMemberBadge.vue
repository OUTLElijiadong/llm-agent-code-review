<script setup lang="ts">
import { computed } from "vue";

const props = withDefaults(defineProps<{
  name: string
  role?: string
  status: string
  address?: string
  /** 卡片内可追问/打开详情的徽章;详情概览只做状态展示。 */
  interactive?: boolean
}>(), {
  interactive: true,
})

const emit = defineEmits<{ click: [] }>()

const statusColor = computed(() => {
  switch (props.status) {
    case "running": return "var(--brand-500, #5b58e8)"
    case "completed": return "var(--color-success, #4fb87a)"
    case "failed": return "var(--color-danger, #dc4961)"
    case "queued":
    case "waiting_dependency": return "var(--gray-400, #9ba3b0)"
    case "reclaimed": return "var(--sev-medium, #d9a857)"
    default: return "var(--gray-300, #c8cdd6)"
  }
})

const roleLabel = computed(() => {
  switch (props.role) {
    case "worker": return ""
    case "verifier": return "验证"
    case "summarizer": return "汇总"
    default: return ""
  }
})

const agentCode = computed(() => {
  if (!props.address) return ""
  const match = props.address.match(/agent:(\w+)/)
  return match?.[1] ?? ""
})

function activate(): void {
  if (props.interactive) emit("click")
}
</script>

<template>
  <span
    class="team-member-badge"
    :class="[`status-${status}`, { 'is-interactive': props.interactive }]"
    :role="props.interactive ? 'button' : undefined"
    :tabindex="props.interactive ? 0 : undefined"
    :aria-label="props.interactive ? `${name || agentCode || '子Agent'}${roleLabel ? `,${roleLabel}` : ''}` : undefined"
    @click.stop="activate"
    @keydown.enter.stop.prevent="activate"
    @keydown.space.stop.prevent="activate"
  >
    <span class="badge-dot" :style="{ background: statusColor }" />
    <span class="badge-name">{{ name || agentCode }}</span>
    <span v-if="roleLabel" class="badge-role">{{ roleLabel }}</span>
  </span>
</template>

<style scoped>
.team-member-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 500;
  transition: all 0.15s ease;
  border: 1px solid #e5e7eb;
  background: #fff;
  color: #374151;
  white-space: nowrap;
}
.team-member-badge.is-interactive {
  cursor: pointer;
}
.team-member-badge.is-interactive:hover,
.team-member-badge.is-interactive:focus-visible {
  border-color: var(--brand-300, #8e88f5);
  background: var(--brand-50, #EFEEFE);
}
.team-member-badge.is-interactive:focus-visible {
  outline: 2px solid var(--brand-300, #8e88f5);
  outline-offset: 2px;
}
.badge-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex-shrink: 0;
}
.status-running .badge-dot {
  animation: dot-pulse 1.5s ease-in-out infinite;
}
@keyframes dot-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}
.badge-name {
  color: #1f2937;
}
.badge-role {
  color: #9ca3af;
  font-size: 11px;
}
.status-completed {
  border-color: var(--color-success, #4fb87a);
  background: var(--color-success-light, #e5f4ec);
}
.status-failed {
  border-color: var(--color-danger, #dc4961);
  background: var(--color-danger-light, #fceaee);
}
</style>
