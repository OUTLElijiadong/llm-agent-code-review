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
    <!-- 统一的行动指引:有 actionText 就显示一个主按钮,点击跳路由或触发自定义 -->
    <div v-else-if="actionText" class="empty-action">
      <button type="button" class="empty-action-btn" @click="onAction">{{ actionText }}</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'

const props = withDefaults(defineProps<{
  description?: string
  compact?: boolean
  imageSize?: number
  /** 行动按钮文案,如「创建项目」「返回列表」 */
  actionText?: string
  /** 点击后跳转的站内路由(与 actionHandler 二选一) */
  actionTo?: string
}>(), {
  description: '暂无数据',
  compact: false,
  imageSize: 80,
  actionText: '',
  actionTo: '',
})

const emit = defineEmits<{ action: [] }>()
const router = useRouter()

function onAction(): void {
  emit('action')
  if (props.actionTo) void router.push(props.actionTo)
}
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

.empty-action-btn {
  min-height: 34px;
  padding: 0 var(--sp-4, 16px);
  border: 1px solid var(--brand-300, #8E88F5);
  border-radius: var(--r-md, 8px);
  background: var(--brand-500, #5B58E8);
  color: #fff;
  font-size: 13px;
  cursor: pointer;
  transition: all var(--transition-fast, 0.15s ease);
}
.empty-action-btn:hover {
  background: var(--brand-600, #4A46D4);
  border-color: var(--brand-600, #4A46D4);
  transform: translateY(-1px);
}
@media (prefers-reduced-motion: reduce) {
  .empty-action-btn { transition: none; }
  .empty-action-btn:hover { transform: none; }
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
