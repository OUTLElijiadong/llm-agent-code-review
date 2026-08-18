<template>
  <div
    class="prism-loading"
    :class="{ compact, overlay }"
    role="status"
    aria-live="polite"
    :aria-label="label"
  >
    <AiOrb :size="compact ? 44 : 72" state="thinking" />
    <div class="loader-text">
      <div class="loader-label font-display">{{ label }}</div>
      <div v-if="sublabel" class="loader-sub">
        <span class="think-dots"><span></span><span></span><span></span></span>
        <span class="font-mono">{{ sublabel }}</span>
      </div>
    </div>
    <FluidProgress class="loader-fluid" indeterminate :height="5" />
  </div>
</template>

<script setup lang="ts">
import AiOrb from '@/components/common/AiOrb.vue'
import FluidProgress from '@/components/common/FluidProgress.vue'

withDefaults(defineProps<{
  label?: string
  sublabel?: string
  compact?: boolean
  overlay?: boolean
}>(), {
  label: '正在加载数据',
  sublabel: 'Agent 正在整理页面内容',
  compact: false,
  overlay: false,
})
</script>

<style scoped lang="scss">
.prism-loading {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  min-height: 220px;
  padding: 32px 24px;
  background:
    radial-gradient(circle at 50% 34%, rgba(91, 88, 232, 0.10), transparent 32%),
    linear-gradient(135deg, #fff, #F7F8FA);
  border: 1px solid #EEF0F4;
  border-radius: 12px;
  overflow: hidden;

  &.compact {
    min-height: 96px;
    padding: 18px 16px;
    gap: 10px;
  }

  &.overlay {
    min-height: auto;
    width: min(360px, calc(100vw - 48px));
    box-shadow: 0 24px 48px -12px rgba(37, 42, 55, 0.18),
                0 0 0 1px rgba(91, 88, 232, 0.04);
    border-color: rgba(91, 88, 232, 0.14);
  }
}

.loader-text {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  text-align: center;
  min-width: 0;
}

.loader-label {
  font-size: 15px;
  font-weight: 600;
  color: #161A24;
}

.loader-sub {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: #6E7689;
}

.loader-fluid {
  width: min(280px, 100%);
}
</style>
