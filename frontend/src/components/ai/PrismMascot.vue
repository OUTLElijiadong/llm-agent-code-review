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

    <!-- 圆耳朵(可爱化:柔软的圆角小耳) -->
    <path d="M20 14.5 Q17 8 23.5 9.5 Q28 10.5 26.5 16.5 Z" fill="#8F8BFF" stroke="#3E3AA6" stroke-opacity="0.3" stroke-width="1.2" stroke-linejoin="round" />
    <path d="M44 14.5 Q47 8 40.5 9.5 Q36 10.5 37.5 16.5 Z" fill="#8F8BFF" stroke="#3E3AA6" stroke-opacity="0.3" stroke-width="1.2" stroke-linejoin="round" />
    <path d="M21.6 13.6 Q20.6 10.8 23.4 11.4 Q25.2 11.9 24.4 14.6 Z" fill="#FFB3C7" fill-opacity="0.7" />
    <path d="M42.4 13.6 Q43.4 10.8 40.6 11.4 Q38.8 11.9 39.6 14.6 Z" fill="#FFB3C7" fill-opacity="0.7" />

    <!-- 头顶小星光(可爱化:随身小星星伙伴) -->
    <g class="prismling-star">
      <path d="M32 2.6 L33.1 5.4 L36 5.6 L33.8 7.5 L34.5 10.3 L32 8.8 L29.5 10.3 L30.2 7.5 L28 5.6 L30.9 5.4 Z" fill="#FFD66E" />
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

    <!-- 表情(可爱化:亮晶晶大眼 + 猫猫嘴 + 软腮红) -->
    <g class="prismling-face">
      <g class="prismling-eyes">
        <ellipse cx="25.5" cy="33.2" rx="2.9" ry="3.2" fill="#FFFFFF" />
        <ellipse cx="38.5" cy="33.2" rx="2.9" ry="3.2" fill="#FFFFFF" />
        <circle cx="24.7" cy="32.1" r="1" fill="#B9F0FA" fill-opacity="0.95" />
        <circle cx="37.7" cy="32.1" r="1" fill="#B9F0FA" fill-opacity="0.95" />
        <circle cx="26.3" cy="34.4" r="0.5" fill="#FFFFFF" />
        <circle cx="39.3" cy="34.4" r="0.5" fill="#FFFFFF" />
      </g>
      <path
        class="prismling-mouth"
        d="M27.5 40.2 Q29.7 42.8 32 40.2 Q34.3 42.8 36.5 40.2"
        stroke="#FFFFFF"
        stroke-width="2"
        stroke-linecap="round"
        fill="none"
      />
      <ellipse cx="19.8" cy="38.6" rx="2.7" ry="2.1" fill="#FFB3C7" fill-opacity="0.8" />
      <ellipse cx="44.2" cy="38.6" rx="2.7" ry="2.1" fill="#FFB3C7" fill-opacity="0.8" />
    </g>

    <!-- 身旁闪烁小星(可爱化:陪伴星星) -->
    <g class="prismling-sparkles">
      <path d="M8.5 12 L9.3 14 L11.3 14.8 L9.3 15.6 L8.5 17.6 L7.7 15.6 L5.7 14.8 L7.7 14 Z" fill="#FFD66E" fill-opacity="0.9" />
      <path d="M56 44 L56.6 45.5 L58.1 46.1 L56.6 46.7 L56 48.2 L55.4 46.7 L53.9 46.1 L55.4 45.5 Z" fill="#7EE3F0" fill-opacity="0.9" />
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

/* 可爱化:头顶小星随呼吸轻晃,身旁星星交替闪烁 */
.prismling-star {
  transform-origin: 32px 6px;
  animation: prismling-star-sway 2.6s ease-in-out infinite;
}

.prismling-sparkles {
  animation: prismling-twinkle 2.2s ease-in-out infinite;
}

.is-running .prismling-sparkles {
  animation-duration: 1.2s;
}

@keyframes prismling-star-sway {
  0%, 100% { transform: rotate(-8deg) translateY(0); }
  50% { transform: rotate(8deg) translateY(-0.8px); }
}

@keyframes prismling-twinkle {
  0%, 100% { opacity: 0.35; transform: scale(0.86); }
  50% { opacity: 1; transform: scale(1.08); }
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
  .prismling-attention,
  .prismling-star,
  .prismling-sparkles {
    animation: none !important;
  }
}
</style>
