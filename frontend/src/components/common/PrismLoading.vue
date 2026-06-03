<template>
  <div
    class="prism-loading"
    :class="{ compact, overlay }"
    role="status"
    aria-live="polite"
    :aria-label="label"
  >
    <div class="loader-core" aria-hidden="true">
      <span class="prism-ring"></span>
      <span class="prism-mark on-light"></span>
    </div>
    <div class="loader-text">
      <div class="loader-label font-display">{{ label }}</div>
      <div v-if="sublabel" class="loader-sub">
        <span class="think-dots"><span></span><span></span><span></span></span>
        <span class="font-mono">{{ sublabel }}</span>
      </div>
    </div>
    <div class="loader-beam" aria-hidden="true">
      <span></span>
    </div>
  </div>
</template>

<script setup lang="ts">
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

.loader-core {
  position: relative;
  width: 64px;
  height: 64px;
  display: grid;
  place-items: center;

  .prism-mark {
    width: 30px;
    height: 30px;
    border-radius: 9px;
    z-index: 2;
  }

  .on-light {
    background: linear-gradient(135deg, #8E88F5, #5B58E8);
    box-shadow: 0 2px 8px rgba(91, 88, 232, 0.25);
  }
}

.compact .loader-core {
  width: 44px;
  height: 44px;

  .prism-mark {
    width: 22px;
    height: 22px;
    border-radius: 7px;
  }
}

.prism-ring {
  position: absolute;
  inset: 0;
  border-radius: 50%;
  background: conic-gradient(from 210deg,
    transparent,
    #6B7CFF,
    #2BBFB9,
    #D4A53A,
    #E25C73,
    transparent);
  animation: prismSpin 1.4s linear infinite;

  &::after {
    content: '';
    position: absolute;
    inset: 7px;
    border-radius: 50%;
    background: #fff;
    box-shadow: inset 0 0 0 1px #EEF0F4;
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

.loader-beam {
  position: relative;
  width: min(280px, 100%);
  height: 4px;
  border-radius: 999px;
  background: #DCDAFD;
  overflow: hidden;

  span {
    position: absolute;
    inset: 0 auto 0 -45%;
    width: 45%;
    border-radius: inherit;
    background: linear-gradient(90deg, transparent, #6F69EE, #3DBCD9, transparent);
    animation: prismBeam 1.35s ease-in-out infinite;
  }
}

@keyframes prismSpin {
  to { transform: rotate(360deg); }
}

@keyframes prismBeam {
  0% { left: -45%; }
  100% { left: 100%; }
}
</style>
