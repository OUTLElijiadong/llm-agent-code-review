<script setup lang="ts">
/**
 * Prism 平台吉祥物「小菱」(Prismling)——一个小棱镜小人偶。
 * 运行中会旋转追光并眨眼,等待交互时显示提示点,空闲时安静呼吸。
 */
interface Props {
  size?: number
  /** 运行状态:idle 空闲 / running 运行中 / waiting 等待用户操作 */
  status?: 'idle' | 'running' | 'waiting'
}

withDefaults(defineProps<Props>(), {
  size: 56,
  status: 'idle',
})
</script>

<template>
  <svg
    class="prismling"
    :class="`is-${status}`"
    :width="size"
    :height="size"
    viewBox="0 0 64 64"
    fill="none"
    aria-hidden="true"
  >
    <defs>
      <linearGradient id="prismling-body" x1="14" y1="14" x2="50" y2="52" gradientUnits="userSpaceOnUse">
        <stop offset="0" stop-color="#8F8BFF" />
        <stop offset="0.55" stop-color="#5B58E8" />
        <stop offset="1" stop-color="#3DBCD9" />
      </linearGradient>
      <linearGradient id="prismling-beam" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0" stop-color="#FFD66E" />
        <stop offset="0.55" stop-color="#7EE3F0" />
        <stop offset="1" stop-color="#8F8BFF" stop-opacity="0.4" />
      </linearGradient>
    </defs>

    <!-- 折射光束 -->
    <g class="prismling-beams" stroke-linecap="round">
      <path d="M5 25 L16 28.5" stroke="url(#prismling-beam)" stroke-width="2.2" />
      <path d="M48 21 L59 16.5" stroke="#FFD66E" stroke-width="2" />
      <path d="M49.5 27 L61 27.5" stroke="#7EE3F0" stroke-width="2" />
      <path d="M48 33 L58.5 38" stroke="#B9B4FF" stroke-width="2" />
    </g>

    <!-- 小脚 -->
    <rect x="20.5" y="52" width="7" height="5" rx="2.5" fill="#4540B8" />
    <rect x="36.5" y="52" width="7" height="5" rx="2.5" fill="#4540B8" />

    <!-- 棱镜身体 -->
    <path
      d="M32 9 L53 47.5 Q53.8 49.6 51.9 49.6 L12.1 49.6 Q10.2 49.6 11 47.5 Z"
      fill="url(#prismling-body)"
      stroke="#3E3AA6"
      stroke-opacity="0.35"
      stroke-width="1.5"
      stroke-linejoin="round"
    />
    <!-- 身体高光 -->
    <path d="M32 13 L40 29 L32 46 L24 29 Z" fill="#FFFFFF" fill-opacity="0.14" />
    <path d="M32 13 L24 29 L14.5 46" stroke="#FFFFFF" stroke-opacity="0.35" stroke-width="1.2" />

    <!-- 表情 -->
    <g class="prismling-face">
      <g class="prismling-eyes">
        <circle cx="25.5" cy="33.5" r="2.6" fill="#FFFFFF" />
        <circle cx="38.5" cy="33.5" r="2.6" fill="#FFFFFF" />
      </g>
      <path
        class="prismling-mouth"
        d="M27.5 40.5 Q32 44 36.5 40.5"
        stroke="#FFFFFF"
        stroke-width="2.2"
        stroke-linecap="round"
      />
      <circle cx="20.5" cy="38.5" r="2" fill="#FFB3C7" fill-opacity="0.85" />
      <circle cx="43.5" cy="38.5" r="2" fill="#FFB3C7" fill-opacity="0.85" />
    </g>

    <!-- 等待用户操作时的提示点 -->
    <g v-if="status === 'waiting'" class="prismling-attention">
      <circle cx="51" cy="13" r="6.5" fill="#D9A857" stroke="#FFFFFF" stroke-width="2" />
      <text x="51" y="16.5" text-anchor="middle" font-size="9" font-weight="700" fill="#FFFFFF">!</text>
    </g>
  </svg>
</template>

<style scoped>
.prismling {
  display: block;
  transform-origin: 50% 78%;
}

.is-idle {
  animation: prismling-breathe 3.2s ease-in-out infinite;
}

.is-running {
  animation: prismling-bob 0.9s ease-in-out infinite;
}

.is-running .prismling-beams {
  animation: prismling-spin 1.5s linear infinite;
  transform-origin: 32px 32px;
}

.is-running .prismling-face {
  animation: prismling-focus 0.9s ease-in-out infinite;
}

.prismling-eyes {
  transform-origin: 32px 33.5px;
  animation: prismling-blink 4.6s ease-in-out infinite;
}

.is-waiting .prismling-attention {
  animation: prismling-pop 1.1s ease-in-out infinite;
  transform-origin: 51px 13px;
}

@keyframes prismling-breathe {
  0%, 100% { transform: translateY(0) scale(1); }
  50% { transform: translateY(-1.5px) scale(1.015); }
}

@keyframes prismling-bob {
  0%, 100% { transform: translateY(0) rotate(-2deg); }
  50% { transform: translateY(-2.5px) rotate(2deg); }
}

@keyframes prismling-spin {
  to { transform: rotate(360deg); }
}

@keyframes prismling-blink {
  0%, 92%, 100% { transform: scaleY(1); }
  95% { transform: scaleY(0.12); }
}

@keyframes prismling-focus {
  0%, 100% { transform: translateX(0); }
  50% { transform: translateX(1.2px); }
}

@keyframes prismling-pop {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.18); }
}

@media (prefers-reduced-motion: reduce) {
  .prismling,
  .prismling-beams,
  .prismling-eyes,
  .prismling-face,
  .prismling-attention {
    animation: none !important;
  }
}
</style>
