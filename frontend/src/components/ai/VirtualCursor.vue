<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

import { useAgentActivityStore } from '@/stores/agentActivity'

/**
 * 小菱「帮我操作」虚拟鼠标(操作可视化隐喻):
 * 后端直调业务 API,并不真实点击 DOM;这里用一枚品牌紫渐变光标
 * 从屏幕右下滑入主内容区域中心,到达后泛起目标高亮涟漪,
 * 让非技术用户直观感知「小菱正在替我操作页面」。
 */
const store = useAgentActivityStore()

const visible = ref(false)
const x = ref(0)
const y = ref(0)
/** 到达目标后为 true,触发外圈涟漪高亮 */
const arrived = ref(false)
const cursorStyle = computed(() => ({ transform: `translate3d(${x.value}px, ${y.value}px, 0)` }))

let arriveTimer: number | undefined
let startRaf = 0

/** 目标位置:优先当前主内容容器中心,兜底视口中心。 */
function targetPoint(): { x: number; y: number } {
  const el = document.querySelector('.app-main, .layout-main, .app-layout-main, main, #app')
  const rect = el?.getBoundingClientRect()
  if (rect && rect.width > 0 && rect.height > 0) {
    const cx = rect.left + rect.width / 2
    const cy = rect.top + Math.min(rect.height / 2, window.innerHeight * 0.45)
    return {
      x: Math.min(Math.max(cx, 40), window.innerWidth - 40),
      y: Math.min(Math.max(cy, 40), window.innerHeight - 40),
    }
  }
  return { x: window.innerWidth / 2, y: window.innerHeight / 2 }
}

function moveToTarget(): void {
  // 先钉在右下角(无过渡),下一帧再切到目标位置,让 CSS transition 形成平滑滑入
  x.value = window.innerWidth - 72
  y.value = window.innerHeight - 72
  startRaf = window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => {
      const target = targetPoint()
      x.value = target.x
      y.value = target.y
      arriveTimer = window.setTimeout(() => {
        arrived.value = true
      }, 700)
    })
  })
}

watch(
  () => store.isActing,
  (acting) => {
    window.clearTimeout(arriveTimer)
    window.cancelAnimationFrame(startRaf)
    if (acting) {
      arrived.value = false
      visible.value = true
      void nextTick(moveToTarget)
    } else {
      visible.value = false
      arrived.value = false
    }
  },
)

onBeforeUnmount(() => {
  window.clearTimeout(arriveTimer)
  window.cancelAnimationFrame(startRaf)
})
</script>

<template>
  <Transition name="virtual-cursor-fade">
    <div
      v-if="visible"
      class="virtual-cursor"
      :class="{ 'is-arrived': arrived }"
      :style="cursorStyle"
      aria-hidden="true"
    >
      <span v-if="arrived" class="virtual-cursor-ripple"></span>
      <span v-if="arrived" class="virtual-cursor-ripple is-delayed"></span>
      <svg class="virtual-cursor-icon" width="26" height="26" viewBox="0 0 26 26" fill="none">
        <defs>
          <linearGradient id="virtual-cursor-g" x1="3" y1="3" x2="23" y2="23" gradientUnits="userSpaceOnUse">
            <stop offset="0" stop-color="#8E88F5" />
            <stop offset="0.6" stop-color="#5B58E8" />
            <stop offset="1" stop-color="#3DBCD9" />
          </linearGradient>
        </defs>
        <path
          d="M4 3.5 L21.5 12.2 L13.8 14.6 L10.4 22.2 Z"
          fill="url(#virtual-cursor-g)"
          stroke="#FFFFFF"
          stroke-width="1.4"
          stroke-linejoin="round"
        />
        <circle cx="20" cy="5" r="1.6" fill="#FFD66E" />
        <circle cx="23" cy="9.5" r="1.1" fill="#7EE3F0" />
      </svg>
    </div>
  </Transition>
</template>

<style scoped>
.virtual-cursor {
  position: fixed;
  left: 0;
  top: 0;
  z-index: var(--z-index-tooltip);
  pointer-events: none;
  transition: transform 0.65s cubic-bezier(0.22, 0.9, 0.32, 1);
  will-change: transform;
}

.virtual-cursor-icon {
  display: block;
  filter: drop-shadow(0 2px 8px rgba(91, 88, 232, 0.5));
}

/* 到达目标后的轻微悬停浮动 */
.virtual-cursor.is-arrived .virtual-cursor-icon {
  animation: virtual-cursor-hover 1.6s ease-in-out infinite;
}

/* 目标高亮涟漪 */
.virtual-cursor-ripple {
  position: absolute;
  left: 8px;
  top: 8px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  border: 2px solid var(--brand-400);
  opacity: 0;
  animation: virtual-cursor-ripple 1.8s ease-out infinite;
}
.virtual-cursor-ripple.is-delayed {
  animation-delay: 0.9s;
}

.virtual-cursor-fade-enter-active,
.virtual-cursor-fade-leave-active {
  transition: opacity var(--transition-base);
}
.virtual-cursor-fade-enter-from,
.virtual-cursor-fade-leave-to {
  opacity: 0;
}

@keyframes virtual-cursor-ripple {
  0% { transform: translate(-50%, -50%) scale(0.6); opacity: 0.75; }
  100% { transform: translate(-50%, -50%) scale(3.2); opacity: 0; }
}

@keyframes virtual-cursor-hover {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-2px); }
}

@media (prefers-reduced-motion: reduce) {
  .virtual-cursor { transition: none; }
  .virtual-cursor.is-arrived .virtual-cursor-icon { animation: none; }
  .virtual-cursor-ripple { animation: none; opacity: 0.35; transform: translate(-50%, -50%) scale(1.6); }
}
</style>
