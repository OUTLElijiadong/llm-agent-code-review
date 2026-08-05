<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import PrismLoading from '@/components/common/PrismLoading.vue'
import { setupSecurityAlerts } from '@/composables/useSecurityAlerts'

const router = useRouter()
const routeLoading = ref(false)
let showTimer: number | undefined
let hideTimer: number | undefined
/** 安全告警弹窗句柄(仅登录态管理员生效,composable 内部判断) */
let securityAlertsDispose: (() => void) | null = null

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

onMounted(() => {
  securityAlertsDispose = setupSecurityAlerts().dispose
})

onBeforeUnmount(() => {
  window.clearTimeout(showTimer)
  window.clearTimeout(hideTimer)
  removeBeforeGuard()
  removeAfterGuard()
  removeErrorGuard()
  // 关闭安全告警 SSE 订阅,避免页面卸载后仍持续弹窗
  securityAlertsDispose?.()
  securityAlertsDispose = null
})
</script>

<template>
  <router-view />
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
