<script setup lang="ts">
import { computed } from 'vue'

import { useAgentActivityStore } from '@/stores/agentActivity'
import PrismMascot from '@/components/ai/PrismMascot.vue'

/**
 * 小菱「帮我操作」全屏彩框:
 * 页面操作类工具执行期间,在页面四周渲染光谱渐变流动边框,
 * 右上角浮动「小菱工作中」徽标,让非技术用户明确感知小菱正在代操作。
 */
const store = useAgentActivityStore()

const badgeLabel = computed(() => store.current?.label || '小菱正在帮你操作页面…')
</script>

<template>
  <Transition name="agent-activity-fade">
    <div v-if="store.isActing" class="agent-activity-border">
      <span class="agent-activity-glow" aria-hidden="true"></span>
      <span class="agent-activity-edge edge-top" aria-hidden="true"></span>
      <span class="agent-activity-edge edge-right" aria-hidden="true"></span>
      <span class="agent-activity-edge edge-bottom" aria-hidden="true"></span>
      <span class="agent-activity-edge edge-left" aria-hidden="true"></span>
      <div class="agent-activity-badge" role="status" aria-live="polite">
        <PrismMascot :size="22" status="running" />
        <span class="agent-activity-badge-text">
          <b>小菱工作中</b>
          <small>{{ badgeLabel }}</small>
        </span>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.agent-activity-border {
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: var(--z-index-fixed);
}

/* 内层光谱条:四边流动的光带 */
.agent-activity-edge {
  position: absolute;
  background-image: repeating-linear-gradient(
    90deg,
    #6B7CFF 0 48px,
    #4B9BFF 48px 96px,
    #2BBFB9 96px 144px,
    #4FB87A 144px 192px,
    #D4A53A 192px 240px,
    #E08648 240px 288px,
    #E25C73 288px 336px,
    #B85AC4 336px 384px,
    #6B7CFF 384px 432px
  );
  background-size: 432px 100%;
  animation: agent-activity-flow 5s linear infinite;
  opacity: 0.95;
}

/* 外层彩色柔光:让整圈边框「泛起」彩光,远看也醒目 */
.agent-activity-glow {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(120px 60px at 0% 0%, rgba(107, 124, 255, 0.28), transparent 70%),
    radial-gradient(120px 60px at 100% 0%, rgba(75, 155, 255, 0.26), transparent 70%),
    radial-gradient(120px 60px at 100% 100%, rgba(226, 92, 115, 0.26), transparent 70%),
    radial-gradient(120px 60px at 0% 100%, rgba(184, 90, 196, 0.26), transparent 70%);
  animation: agent-activity-glow-breathe 2.4s ease-in-out infinite;
}
@keyframes agent-activity-glow-breathe {
  0%, 100% { opacity: 0.5; }
  50% { opacity: 1; }
}

.edge-top,
.edge-bottom {
  left: 0;
  right: 0;
  height: 5px;
}
.edge-top { top: 0; }
.edge-bottom { bottom: 0; animation-direction: reverse; }

.edge-left,
.edge-right {
  top: 0;
  bottom: 0;
  width: 5px;
  background-image: repeating-linear-gradient(
    180deg,
    #6B7CFF 0 48px,
    #4B9BFF 48px 96px,
    #2BBFB9 96px 144px,
    #4FB87A 144px 192px,
    #D4A53A 192px 240px,
    #E08648 240px 288px,
    #E25C73 288px 336px,
    #B85AC4 336px 384px,
    #6B7CFF 384px 432px
  );
  background-size: 100% 432px;
}
.edge-left { left: 0; animation-name: agent-activity-flow-y; }
.edge-right { right: 0; animation-name: agent-activity-flow-y; animation-direction: reverse; }

.agent-activity-badge {
  position: absolute;
  top: 10px;
  right: 14px;
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  max-width: min(320px, calc(100vw - 48px));
  padding: 5px var(--sp-3) 5px var(--sp-2);
  border-radius: 999px;
  background: var(--surface-glass);
  border: 1px solid var(--brand-200);
  box-shadow: var(--shadow-2);
  backdrop-filter: blur(6px);
  animation: agent-activity-float 2.4s ease-in-out infinite;
}

.agent-activity-badge-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
  line-height: 1.25;
}

.agent-activity-badge-text b {
  font-size: var(--fs-xs);
  color: var(--brand-700);
  font-weight: 650;
}

.agent-activity-badge-text small {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.agent-activity-fade-enter-active,
.agent-activity-fade-leave-active {
  transition: opacity var(--transition-base);
}
.agent-activity-fade-enter-from,
.agent-activity-fade-leave-to {
  opacity: 0;
}

@keyframes agent-activity-flow {
  to { background-position-x: 432px; }
}
@keyframes agent-activity-flow-y {
  to { background-position-y: 432px; }
}
@keyframes agent-activity-float {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-3px); }
}

@media (prefers-reduced-motion: reduce) {
  .agent-activity-edge {
    animation: none;
    opacity: 0.55;
    background-image: none;
    background-color: var(--brand-400);
  }
  .agent-activity-badge { animation: none; }
  .agent-activity-glow { animation: none; opacity: 0.4; }
}
</style>
