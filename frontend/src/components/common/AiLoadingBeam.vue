<template>
  <div class="ai-beam" :class="{ ribbon }">
    <div class="beam-track">
      <FluidProgress indeterminate :height="10" />
    </div>
    <div v-if="!ribbon" class="beam-meta">
      <AiOrb :size="26" state="thinking" :halo="false" />
      <div class="beam-text">
        <div class="beam-title">{{ title }}</div>
        <div class="beam-sub">
          <span class="think-dots"><span></span><span></span><span></span></span>
          <span class="font-mono">{{ status }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import AiOrb from '@/components/common/AiOrb.vue'
import FluidProgress from '@/components/common/FluidProgress.vue'

withDefaults(defineProps<{
  title?: string
  status?: string
  ribbon?: boolean
}>(), {
  title: 'Agent 正在审查代码',
  status: 'DeepSeek V4 · 流式输出中',
  ribbon: false,
})
</script>

<style scoped lang="scss">
.ai-beam {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 18px 20px;
  background: linear-gradient(135deg, #FAFAFE, #F0EFFE);
  border: 1px solid #DCDAFD;
  border-radius: 12px;

  &.ribbon {
    padding: 0;
    background: transparent;
    border: none;
  }
}

.beam-track {
  position: relative;
  height: 10px;
  border-radius: 5px;
  overflow: hidden;
}

/* AI 思考球紧凑对齐 */
.beam-meta .ai-orb {
  margin-top: -2px;
}

.beam-meta {
  display: flex;
  align-items: center;
  gap: 12px;
}

.beam-text {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  flex: 1;
}

.beam-title {
  font-size: 14px;
  font-weight: 600;
  color: #161A24;
}

.beam-sub {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 11px;
  color: #6E7689;
}
</style>
