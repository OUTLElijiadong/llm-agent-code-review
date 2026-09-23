<script setup lang="ts">
import { computed, getCurrentInstance } from 'vue'

/** 小菱：保留棱镜识别度的圆润小精灵。状态同时由表情和符号表达。 */
const props = withDefaults(defineProps<{
  size?: number
  status?: 'idle' | 'thinking' | 'working' | 'running' | 'waiting' | 'error'
  /** 嵌入已命名按钮时保持装饰性；独立展示可开启可读状态。 */
  decorative?: boolean
  label?: string
}>(), { size: 56, status: 'idle', decorative: true, label: '小菱' })

const state = computed(() => props.status === 'running' ? 'working' : props.status)
const stateLabel = computed(() => ({
  idle: '准备好了', thinking: '正在思考', working: '正在工作', waiting: '等待你操作', error: '遇到问题',
})[state.value])
// 同页消息头像与浮窗会重复使用组件，渐变引用必须属于当前实例。
const bodyId = `prismling-body-${getCurrentInstance()!.uid}`
</script>

<template>
  <svg
    class="prismling"
    :class="`is-${state}`"
    :data-state="state"
    :width="size"
    :height="size"
    viewBox="0 0 64 64"
    fill="none"
    focusable="false"
    :role="decorative ? undefined : 'img'"
    :aria-hidden="decorative ? 'true' : undefined"
    :aria-label="decorative ? undefined : `${label} · ${stateLabel}`"
  >
    <defs>
      <linearGradient :id="bodyId" x1="17" y1="13" x2="46" y2="54" gradientUnits="userSpaceOnUse">
        <stop stop-color="#B2A5FF" />
        <stop offset="0.52" stop-color="#8174EF" />
        <stop offset="1" stop-color="#58C9D5" />
      </linearGradient>
    </defs>
    <ellipse class="prismling-shadow" cx="32" cy="58" rx="13" ry="2.6" fill="#7362CD" opacity=".15" />
    <g class="prismling-float">
      <!-- 小光翼与短手让轮廓柔软，中心仍是四角菱形。 -->
      <path d="M15 27C5 19 3 30 12 36M49 27C59 19 61 30 52 36" fill="#CBF4F3" stroke="#94DCD9" stroke-width="1.3" stroke-linejoin="round" />
      <path class="prismling-body" d="M27 10Q32 5 37 10L52 25Q58 31 52 38L38 52Q32 58 26 52L12 38Q6 31 12 25Z" :fill="`url(#${bodyId})`" stroke="#7165CC" stroke-width="1.4" />
      <path d="M29 12Q32 9 35 12L43 20Q32 15 21 23Z" fill="white" opacity=".34" />
      <path d="M33 46L41 43L35 51Q32 54 29 51Z" fill="#A9F1EC" opacity=".45" />
      <ellipse cx="32" cy="32.5" rx="17.2" ry="14" fill="#F8F7FF" />
      <ellipse cx="20.5" cy="37" rx="3.2" ry="2.1" fill="#F5B6CF" opacity=".8" />
      <ellipse cx="43.5" cy="37" rx="3.2" ry="2.1" fill="#F5B6CF" opacity=".8" />
      <g class="prismling-eyes" fill="#443867">
        <template v-if="state === 'error'">
          <path d="M22 28L28 30M36 30L42 28" stroke="#443867" stroke-width="1.8" stroke-linecap="round" />
          <ellipse cx="25" cy="33" rx="2" ry="2.5" />
          <ellipse cx="39" cy="33" rx="2" ry="2.5" />
        </template>
        <template v-else>
          <ellipse :cx="state === 'thinking' ? 26 : 25" cy="30.5" rx="2.8" ry="3.6" />
          <ellipse :cx="state === 'thinking' ? 40 : 39" cy="30.5" rx="2.8" ry="3.6" />
          <circle :cx="state === 'thinking' ? 25.2 : 24.2" cy="29.3" r="1" fill="white" />
          <circle :cx="state === 'thinking' ? 39.2 : 38.2" cy="29.3" r="1" fill="white" />
        </template>
      </g>
      <path v-if="state === 'error'" d="M29 40Q32 37 35 40" stroke="#69538A" stroke-width="1.8" stroke-linecap="round" />
      <ellipse v-else-if="state === 'thinking' || state === 'waiting'" cx="32" cy="38.5" rx="2" ry="2.4" fill="#8A6DAB" />
      <path v-else d="M28 37Q32 42 36 37" stroke="#69538A" stroke-width="1.9" stroke-linecap="round" />
      <path class="prismling-hand" d="M12 35Q6 38 10 41Q13 42 16 39M49 38Q53 42 56 38" stroke="#9385E8" stroke-width="3.6" stroke-linecap="round" />
      <path class="prismling-crown" d="M29 6L32 1.8L35 6L32 9Z" fill="#F9CF72" stroke="#E9B95A" stroke-width=".7" stroke-linejoin="round" />
    </g>
    <g v-if="state === 'thinking'" class="prismling-thought" fill="#A28BDD">
      <circle cx="51" cy="18" r="1.5" /><circle cx="55" cy="13" r="2" /><circle cx="58" cy="6.5" r="2.6" />
    </g>
    <g v-else-if="state === 'waiting' || state === 'error'" class="prismling-attention">
      <circle cx="52" cy="12" r="8" :fill="state === 'error' ? '#C95E78' : '#CA9643'" stroke="white" stroke-width="2" />
      <path d="M52 8V12" stroke="white" stroke-width="2" stroke-linecap="round" /><circle cx="52" cy="15.3" r="1" fill="white" />
    </g>
    <g v-else-if="state === 'working'" class="prismling-work-spark" fill="#F3C25E">
      <path d="M53 5L55 10L60 12L55 14L53 19L51 14L46 12L51 10Z" />
      <path d="M7 12L8 15L11 16L8 17L7 20L6 17L3 16L6 15Z" />
    </g>
  </svg>
</template>

<style scoped>
.prismling { display: block; flex-shrink: 0; overflow: visible; }
.prismling-float { transform-origin: 32px 34px; animation: prismling-breathe 4s ease-in-out infinite; }
.prismling-eyes { transform-origin: 32px 31px; animation: prismling-blink 5.4s ease-in-out infinite; }
.is-thinking .prismling-float { animation-duration: 3s; }
.is-thinking .prismling-thought { animation: prismling-thought 2s ease-in-out infinite; }
.is-working .prismling-float { animation: prismling-work 1.8s ease-in-out infinite; }
.is-working .prismling-work-spark { transform-origin: 53px 12px; animation: prismling-thought 1.8s ease-in-out infinite; }
.is-waiting .prismling-float { animation-duration: 5s; }
.is-error .prismling-float, .is-error .prismling-eyes { animation: none; }
@keyframes prismling-breathe { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-1.3px); } }
@keyframes prismling-blink { 0%, 91%, 97%, 100% { transform: scaleY(1); } 94% { transform: scaleY(.15); } }
@keyframes prismling-work { 0%, 100% { transform: translateY(0) rotate(-2deg); } 50% { transform: translateY(-1.8px) rotate(2deg); } }
@keyframes prismling-thought { 0%, 100% { opacity: .5; } 50% { opacity: 1; } }
@media (prefers-reduced-motion: reduce) {
  .prismling *, .prismling { animation: none !important; transition: none !important; }
}
</style>
