<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, provide, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import PrismLoading from '@/components/common/PrismLoading.vue'
import AgentActivityBorder from '@/components/ai/AgentActivityBorder.vue'
import VirtualCursor from '@/components/ai/VirtualCursor.vue'
import { useUserStore } from '@/stores/user'
import { setupSecurityAlerts, type SecurityAlertsHandle } from '@/composables/useSecurityAlerts'

const AgentChatDrawer = defineAsyncComponent(() => import('@/components/ai/AgentChatDrawer.vue'))
const AdminCopilot = defineAsyncComponent(() => import('@/components/admin/AdminCopilot.vue'))
const GlobalDiscussionHost = defineAsyncComponent(() => import('@/components/agent/GlobalDiscussionHost.vue'))

const router = useRouter()
const userStore = useUserStore()
const routeLoading = ref(false)
const agentVisible = ref(false)
const agentPrefill = ref('')
const agentPreferredSessionId = ref('')
const agentSessionRequestId = ref(0)
let showTimer: number | undefined
let hideTimer: number | undefined
let securityAlerts: SecurityAlertsHandle | null = null
const canUseAgent = computed(() => (
  Boolean(userStore.token && userStore.profile)
  && userStore.hasPermission('agent:chat')
))

const AGENT_VISIBLE_PREFIX = 'prism-agent-drawer-visible:'

function agentVisibleStorageKey(): string {
  const id = userStore.profile?.id
  return id ? `${AGENT_VISIBLE_PREFIX}${id}` : ''
}

// 浮窗打开状态按账号持久化:此前只存内存,退出登录后组件卸载,
// 重新登录时状态归零,用户感觉"悬浮窗不见了"。
watch(agentVisible, (val) => {
  if (!val) agentPreferredSessionId.value = ''
  const key = agentVisibleStorageKey()
  if (!key) return
  try {
    if (val) window.localStorage.setItem(key, '1')
    else window.localStorage.removeItem(key)
  } catch {
    // localStorage 不可用时退化为当前页面生命周期内记忆
  }
})

watch([canUseAgent, () => userStore.profile?.id], ([allowed, id]) => {
  if (!allowed || !id) {
    agentVisible.value = false
    return
  }
  const key = agentVisibleStorageKey()
  if (!key) return
  try {
    agentVisible.value = window.localStorage.getItem(key) === '1'
  } catch {
    // 读取失败保持默认关闭
  }
}, { immediate: true })

/** 两个权限隔离的会话 surface 共用小菱主控；管理员工具仍按当前角色授权。 */
function openAgentChat(prefill = '', preferredSessionId = ''): void {
  if (!canUseAgent.value) return
  if (userStore.isAdmin()) {
    window.dispatchEvent(new Event('prism:close-admin-copilot'))
  }
  if (prefill) agentPrefill.value = prefill
  agentPreferredSessionId.value = preferredSessionId
  agentSessionRequestId.value += 1
  agentVisible.value = true
}

function handleAdminCopilotOpened(): void {
  agentVisible.value = false
}

function handleOpenAgentChat(event: Event): void {
  const detail = (event as CustomEvent<{ prefill?: string; sessionId?: string }>).detail
  openAgentChat(detail?.prefill ?? '', detail?.sessionId ?? '')
}

provide('openAgentChat', () => openAgentChat())

// 安全告警需要常驻在全局宿主中，不能依赖管理员是否正好打开监控页。
// 账号、令牌或管理角色变化时先关闭旧 SSE，再为新身份单独建立订阅。
const stopSecurityAlertWatch = watch(
  [() => userStore.token, () => userStore.profile?.id, () => userStore.isAdmin()],
  ([token, userId, isAdmin]) => {
    securityAlerts?.dispose()
    securityAlerts = null
    if (token && userId && isAdmin) securityAlerts = setupSecurityAlerts()
  },
  { immediate: true },
)

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
  window.addEventListener('prism:admin-copilot-opened', handleAdminCopilotOpened)
})

onBeforeUnmount(() => {
  stopSecurityAlertWatch()
  securityAlerts?.dispose()
  securityAlerts = null
  window.clearTimeout(showTimer)
  window.clearTimeout(hideTimer)
  removeBeforeGuard()
  removeAfterGuard()
  removeErrorGuard()
  window.removeEventListener('prism:open-agent-chat', handleOpenAgentChat as EventListener)
  window.removeEventListener('prism:admin-copilot-opened', handleAdminCopilotOpened)
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
  <AdminCopilot v-if="canUseAgent && userStore.isAdmin()" />
  <AgentChatDrawer
    v-if="canUseAgent"
    v-model:visible="agentVisible"
    :prefill="agentPrefill"
    :preferred-session-id="agentPreferredSessionId"
    :preferred-session-request-id="agentSessionRequestId"
    :show-launcher="false"
    @consumed-prefill="agentPrefill = ''"
  />
  <GlobalDiscussionHost v-if="userStore.isLoggedIn && userStore.profile" :user-id="userStore.profile.id" />
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
