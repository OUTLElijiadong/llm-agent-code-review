<script setup lang="ts">
import { defineAsyncComponent, onBeforeUnmount, onMounted, provide, ref } from 'vue'
import { useRouter } from 'vue-router'
import PrismLoading from '@/components/common/PrismLoading.vue'
import AgentActivityBorder from '@/components/ai/AgentActivityBorder.vue'
import VirtualCursor from '@/components/ai/VirtualCursor.vue'
import { useUserStore } from '@/stores/user'

const AgentChatDrawer = defineAsyncComponent(() => import('@/components/ai/AgentChatDrawer.vue'))
const AdminCopilot = defineAsyncComponent(() => import('@/components/admin/AdminCopilot.vue'))

const router = useRouter()
const userStore = useUserStore()
const routeLoading = ref(false)
const agentVisible = ref(false)
const agentPrefill = ref('')
let showTimer: number | undefined
let hideTimer: number | undefined

/** 全站唯一小菱入口；管理员始终唤起管理会话，普通成员唤起用户会话。 */
function openAgentChat(prefill = ''): void {
  if (userStore.isAdmin()) {
    window.dispatchEvent(new CustomEvent('prism:open-admin-copilot', { detail: { prefill } }))
    return
  }
  if (prefill) agentPrefill.value = prefill
  agentVisible.value = true
}

function handleOpenAgentChat(event: Event): void {
  const detail = (event as CustomEvent<{ prefill?: string }>).detail
  openAgentChat(detail?.prefill ?? '')
}

provide('openAgentChat', () => openAgentChat())

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
  window.addEventListener('prism:open-agent-chat', handleOpenAgentChat as EventListener)
})

onBeforeUnmount(() => {
  window.clearTimeout(showTimer)
  window.clearTimeout(hideTimer)
  removeBeforeGuard()
  removeAfterGuard()
  removeErrorGuard()
  window.removeEventListener('prism:open-agent-chat', handleOpenAgentChat as EventListener)
})
</script>

<template>
  <router-view v-slot="{ Component }">
    <transition name="page-fade" mode="out-in">
      <component :is="Component" />
    </transition>
  </router-view>
  <AgentActivityBorder />
  <VirtualCursor />
  <AdminCopilot v-if="userStore.token && userStore.profile && userStore.isAdmin()" />
  <AgentChatDrawer
    v-else-if="userStore.token && userStore.profile"
    v-model:visible="agentVisible"
    :prefill="agentPrefill"
    @consumed-prefill="agentPrefill = ''"
  />
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

/* 页面级过渡:淡出旧页 + 新页轻微上浮,120ms 内完成避免拖慢导航 */
.page-fade-enter-active {
  transition: opacity 0.16s ease, transform 0.16s ease;
}

.page-fade-leave-active {
  transition: opacity 0.12s ease;
}

.page-fade-enter-from {
  opacity: 0;
  transform: translateY(6px);
}

.page-fade-leave-to {
  opacity: 0;
}

@media (prefers-reduced-motion: reduce) {
  .page-fade-enter-active,
  .page-fade-leave-active {
    transition: none;
  }

  .page-fade-enter-from {
    transform: none;
  }
}
</style>
