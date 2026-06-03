<script setup lang="ts">
import { computed } from 'vue'
import type { AgentStatus } from '@/types/agent'

interface Props {
  code: string
  size?: number
  color?: string
  status?: AgentStatus
  showRing?: boolean
  label?: string
}

const props = withDefaults(defineProps<Props>(), {
  size: 48,
  color: '#5B58E8',
  status: 'idle',
  showRing: true,
  label: '',
})

const ringStrokeMap: Record<AgentStatus, string> = {
  idle: 'rgba(155, 163, 176, 0.55)',
  thinking: '#5B58E8',
  working: '#3DBCD9',
  blocked: '#D9A857',
  error: '#DC4961',
  offline: 'rgba(155, 163, 176, 0.35)',
}

const ringDashMap: Record<AgentStatus, string> = {
  idle: '0',
  thinking: '6 4',
  working: '0',
  blocked: '2 3',
  error: '0',
  offline: '2 4',
}

const ringStroke = computed(() => ringStrokeMap[props.status])
const ringDash = computed(() => ringDashMap[props.status])
const viewSize = 64
const radius = 28
const center = viewSize / 2

const iconBackground = computed(() => {
  if (props.status === 'offline') {
    return 'linear-gradient(135deg, rgba(155,163,176,0.18), rgba(155,163,176,0.06))'
  }
  return `linear-gradient(135deg, ${props.color}38, ${props.color}10)`
})

const innerStroke = computed(() => {
  if (props.status === 'offline') return 'rgba(155, 163, 176, 0.55)'
  if (props.status === 'error') return '#DC4961'
  return props.color
})
</script>

<template>
  <div
    class="agent-avatar"
    :class="[`status-${status}`]"
    :style="{
      width: `${size}px`,
      height: `${size}px`,
      '--avatar-color': color,
      '--avatar-ring': ringStroke,
      '--avatar-bg': iconBackground,
      '--avatar-stroke': innerStroke,
    }"
    role="img"
    :aria-label="label || code"
  >
    <svg
      v-if="showRing"
      class="ring"
      :viewBox="`0 0 ${viewSize} ${viewSize}`"
      aria-hidden="true"
    >
      <circle
        :cx="center"
        :cy="center"
        :r="radius"
        fill="none"
        :stroke="ringStroke"
        :stroke-dasharray="ringDash"
        stroke-width="1.5"
      />
    </svg>

    <div class="core">
      <svg
        class="glyph"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="1.6"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <!-- 不同 Agent 渲染不同几何图形 -->
        <template v-if="code === 'orchestrator'">
          <polygon points="12,3 21,9 17,20 7,20 3,9" />
          <circle cx="12" cy="12" r="1.6" fill="currentColor" />
        </template>

        <template v-else-if="code === 'chat_assistant'">
          <circle cx="12" cy="12" r="8" />
          <path d="M7 11h10" />
          <path d="M7 14h6" />
        </template>

        <template v-else-if="code === 'language_detector'">
          <polygon points="12,3 20,9 17,19 7,19 4,9" />
          <path d="M8 9l4 8 4-8" />
        </template>

        <template v-else-if="code === 'project_analyzer'">
          <rect x="4" y="4" width="6" height="6" rx="1" />
          <rect x="14" y="4" width="6" height="6" rx="1" />
          <rect x="4" y="14" width="6" height="6" rx="1" />
          <rect x="14" y="14" width="6" height="6" rx="1" />
        </template>

        <template v-else-if="code === 'code_reviewer'">
          <circle cx="10" cy="10" r="6" />
          <path d="M14.5 14.5l5 5" />
          <path d="M8 10h4" />
        </template>

        <template v-else-if="code === 'project_manager'">
          <path d="M3 6a1 1 0 0 1 1-1h5l2 2h8a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z" />
          <path d="M6 12h12" />
        </template>

        <template v-else-if="code === 'review_orchestrator'">
          <circle cx="8" cy="12" r="4" />
          <circle cx="16" cy="12" r="4" />
          <path d="M10 8l4 8" />
        </template>

        <template v-else-if="code === 'code_file_manager'">
          <path d="M6 4h9l3 3v13a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z" />
          <path d="M14 4v4h4" />
          <path d="M8 13h8" />
          <path d="M8 16h5" />
        </template>

        <template v-else-if="code === 'dashboard'">
          <path d="M3 14a9 9 0 0 1 18 0" />
          <path d="M12 14l4-4" />
          <circle cx="12" cy="14" r="1.2" fill="currentColor" />
        </template>

        <template v-else-if="code === 'rule_manager'">
          <path d="M4 6h12" />
          <path d="M4 12h9" />
          <path d="M4 18h12" />
          <path d="M18 5l2 2 3-3" stroke-width="1.4" />
          <path d="M18 11l2 2 3-3" stroke-width="1.4" />
          <path d="M18 17l2 2 3-3" stroke-width="1.4" />
        </template>

        <template v-else-if="code === 'reporter'">
          <path d="M6 4h9l3 3v13a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z" />
          <path d="M15 4v3h3" />
          <path d="M8 12h8M8 16h5M8 9h4" />
        </template>

        <template v-else-if="code === 'ai_prompt'">
          <path d="M5 8l3-3M5 12l3-3M5 16l3-3" />
          <path d="M14 4l-3 16" stroke-width="2" />
          <path d="M16 9l5-2-1 5 4 1" stroke-width="1.4" />
        </template>

        <template v-else-if="code === 'security_sentinel'">
          <!-- 盾牌外形 -->
          <path d="M12 3 L20 6 V12 C20 16 16.5 19.5 12 21 C7.5 19.5 4 16 4 12 V6 Z" />
          <!-- 上方雷达扇形 (扫描动效隐喻) -->
          <path d="M7.5 7.5 A6 6 0 0 1 16.5 7.5" stroke-width="1.2" />
          <!-- 居中锁孔 -->
          <circle cx="12" cy="12" r="1.6" fill="currentColor" />
          <path d="M12 13.4 V16" />
        </template>

        <template v-else>
          <circle cx="12" cy="12" r="6" />
        </template>
      </svg>

      <!-- working 状态的内核脉冲 -->
      <span v-if="status === 'working'" class="pulse" aria-hidden="true"></span>
      <!-- blocked 状态的提示号 -->
      <span v-if="status === 'blocked'" class="badge badge-warn" aria-hidden="true">!</span>
      <!-- error 状态的提示号 -->
      <span v-if="status === 'error'" class="badge badge-err" aria-hidden="true">×</span>
    </div>
  </div>
</template>

<style scoped lang="scss">
.agent-avatar {
  position: relative;
  display: inline-grid;
  place-items: center;
  flex-shrink: 0;
}

.ring {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}

.core {
  position: relative;
  width: 72%;
  height: 72%;
  border-radius: 50%;
  background: var(--avatar-bg);
  display: grid;
  place-items: center;
  color: var(--avatar-stroke);
  transition: transform 0.2s ease;
}

.glyph {
  width: 60%;
  height: 60%;
}

.pulse {
  position: absolute;
  inset: -10%;
  border-radius: 50%;
  border: 1.5px solid var(--avatar-color);
  opacity: 0.45;
  animation: avatar-pulse 1.4s ease-out infinite;
}

.badge {
  position: absolute;
  right: -8%;
  top: -8%;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  font-size: 10px;
  font-weight: 700;
  color: #fff;
  display: grid;
  place-items: center;
  box-shadow: 0 0 0 2px #fff;
}

.badge-warn { background: #D9A857; animation: avatar-blink 1.1s ease-in-out infinite; }
.badge-err  { background: #DC4961; }

/* === 状态动画 === */
.status-idle .ring circle    { animation: avatar-breathe 4s ease-in-out infinite; }
.status-thinking .ring circle{ animation: avatar-dash    1.6s linear infinite; }
.status-working .ring circle { animation: avatar-spin    1.8s linear infinite; transform-origin: center; }
.status-blocked .ring circle { animation: avatar-blink   1.2s ease-in-out infinite; }
.status-error .core          { animation: avatar-crack   0.6s ease-out 1; }
.status-working .core        { animation: avatar-bounce  0.9s ease-in-out infinite; }
.status-offline .core,
.status-offline .glyph       { filter: grayscale(1); opacity: 0.65; }

@keyframes avatar-breathe {
  0%, 100% { opacity: 0.4; }
  50%      { opacity: 0.9; }
}
@keyframes avatar-dash {
  to { stroke-dashoffset: -32; }
}
@keyframes avatar-spin {
  to { transform: rotate(360deg); transform-origin: center; }
}
@keyframes avatar-blink {
  0%, 100% { opacity: 0.4; }
  50%      { opacity: 1; }
}
@keyframes avatar-pulse {
  0%   { transform: scale(0.92); opacity: 0.45; }
  60%  { transform: scale(1.15); opacity: 0.18; }
  100% { transform: scale(1.3); opacity: 0; }
}
@keyframes avatar-bounce {
  0%, 100% { transform: scale(1); }
  50%      { transform: scale(1.06); }
}
@keyframes avatar-crack {
  0%   { transform: scale(1) translateX(0); }
  30%  { transform: scale(1.08) translateX(-1px); }
  70%  { transform: scale(0.97) translateX(1px); }
  100% { transform: scale(1) translateX(0); }
}

@media (prefers-reduced-motion: reduce) {
  .status-idle .ring circle,
  .status-thinking .ring circle,
  .status-working .ring circle,
  .status-blocked .ring circle,
  .status-working .core,
  .pulse,
  .badge-warn {
    animation: none !important;
  }
}
</style>
