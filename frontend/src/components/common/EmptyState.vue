<template>
  <div class="empty-state" :class="{ compact }">
    <div class="empty-illustration" aria-hidden="true">
      <div class="prism-halo"></div>
      <div class="prism-icon"></div>
    </div>
    <div class="empty-desc">{{ description }}</div>
    <div v-if="$slots.default" class="empty-action">
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
withDefaults(defineProps<{
  description?: string
  compact?: boolean
  imageSize?: number
}>(), {
  description: '暂无数据',
  compact: false,
  imageSize: 80,
})
</script>

<style scoped lang="scss">
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 56px 24px;
  color: #6E7689;

  &.compact {
    padding: 24px 12px;
  }
}

.empty-illustration {
  position: relative;
  width: 96px;
  height: 96px;
  margin-bottom: 18px;
}

.prism-halo {
  position: absolute;
  inset: 0;
  border-radius: 50%;
  background: conic-gradient(from 200deg,
    #6B7CFF, #4B9BFF, #2BBFB9,
    #4FB87A, #D4A53A, #E08648,
    #E25C73, #B85AC4, #6B7CFF);
  filter: blur(18px);
  opacity: 0.32;
  animation: prismPulse 4s ease-in-out infinite;
}

.prism-icon {
  position: absolute;
  inset: 26px;
  border-radius: 12px;
  background: conic-gradient(from 210deg,
    #6B7CFF, #4B9BFF, #2BBFB9,
    #4FB87A, #D4A53A, #E08648,
    #E25C73, #B85AC4, #6B7CFF);
  box-shadow: 0 8px 20px -6px rgba(91, 88, 232, 0.32);

  &::after {
    content: '';
    position: absolute;
    inset: 6px;
    border-radius: 6px;
    background: #fff;
  }
}

@keyframes prismPulse {
  0%, 100% { opacity: 0.32; transform: scale(1); }
  50% { opacity: 0.48; transform: scale(1.04); }
}

.empty-desc {
  font-size: 13px;
  color: #6E7689;
  letter-spacing: 0.04em;
}

.empty-action {
  margin-top: 14px;
}

.compact .empty-illustration {
  width: 64px;
  height: 64px;
  margin-bottom: 12px;
}
.compact .prism-icon {
  inset: 16px;
  border-radius: 8px;
  &::after { inset: 4px; border-radius: 4px; }
}
</style>
