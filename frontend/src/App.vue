<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { useRouter } from 'vue-router'
import PrismLoading from '@/components/common/PrismLoading.vue'
import AgentActivityBorder from '@/components/ai/AgentActivityBorder.vue'
import VirtualCursor from '@/components/ai/VirtualCursor.vue'

const router = useRouter()
const routeLoading = ref(false)
let showTimer: number | undefined
let hideTimer: number | undefined

/**
 * 启动路由级加载提示，短跳转延迟展示以避免页面闪烁
 * @returns void
 */
function startRouteLoading(): void {
  window.clearTimeout(hideTimer)
  window.clearTimeout(showTimer)
  showTimer = window.setTimeout(() => {
    routeLoading.value = true
  }, 120)
}

/**
 * 结束路由级加载提示，保留轻微收尾时间让动画自然退出
 * @returns void
 */
function stopRouteLoading(): void {
  window.clearTimeout(showTimer)
  window.clearTimeout(hideTimer)
  hideTimer = window.setTimeout(() => {
    routeLoading.value = false
  }, 160)
}

const removeBeforeGuard = router.beforeEach(() => {
  startRouteLoading()
  return true
})
const removeAfterGuard = router.afterEach(() => {
  stopRouteLoading()
})
const removeErrorGuard = router.onError(() => {
  stopRouteLoading()
})

onBeforeUnmount(() => {
  window.clearTimeout(showTimer)
  window.clearTimeout(hideTimer)
  removeBeforeGuard()
  removeAfterGuard()
  removeErrorGuard()
})
</script>

<template>
  <router-view />
  <AgentActivityBorder />
  <VirtualCursor />
  <transition name="route-loading-fade">
    <div v-if="routeLoading" class="route-loading-mask">
      <PrismLoading
        overlay
        label="正在加载页面"
        sublabel="正在准备视图与数据"
      />
    </div>
  </transition>
</template>

<style scoped lang="scss">
.route-loading-mask {
  position: fixed;
  inset: 0;
  z-index: var(--z-index-loading);
  display: grid;
  place-items: center;
  background: rgba(247, 248, 250, 0.72);
  backdrop-filter: blur(6px);
}

.route-loading-fade-enter-active,
.route-loading-fade-leave-active {
  transition: opacity 0.18s ease;
}

.route-loading-fade-enter-from,
.route-loading-fade-leave-to {
  opacity: 0;
}
</style>
