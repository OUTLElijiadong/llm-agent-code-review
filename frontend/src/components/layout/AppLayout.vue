<script setup lang="ts">
import { onBeforeUnmount, onMounted, provide, ref } from 'vue'
import AppSidebar from './AppSidebar.vue'
import AppHeader from './AppHeader.vue'
import AgentChatDrawer from '@/components/ai/AgentChatDrawer.vue'

const agentVisible = ref(false)
const sidebarVisible = ref(false)

/**
 * 打开 Agent 助手抽屉
 * @returns void
 */
function openAgentChat(): void {
  agentVisible.value = true
}

/**
 * v2.0: 监听全局事件,允许其他视图(如 Agent 办公室)通过 dispatchEvent 唤起聊天
 * @returns void
 */
function handleOpenChat(): void {
  agentVisible.value = true
}

onMounted(() => {
  window.addEventListener('prism:open-agent-chat', handleOpenChat as EventListener)
})

onBeforeUnmount(() => {
  window.removeEventListener('prism:open-agent-chat', handleOpenChat as EventListener)
})

/**
 * 打开或关闭移动端侧边导航
 * @returns void
 */
function toggleSidebar(): void {
  sidebarVisible.value = !sidebarVisible.value
}

/**
 * 关闭移动端侧边导航
 * @returns void
 */
function closeSidebar(): void {
  sidebarVisible.value = false
}

provide('openAgentChat', openAgentChat)
</script>

<template>
  <div class="app-layout">
    <AppSidebar :mobile-open="sidebarVisible" @close="closeSidebar" />
    <transition name="sidebar-mask-fade">
      <div v-if="sidebarVisible" class="sidebar-mask" @click="closeSidebar"></div>
    </transition>
    <div class="app-layout-right">
      <AppHeader @toggle-sidebar="toggleSidebar" />
      <main class="app-layout-main">
        <router-view v-slot="{ Component, route }">
          <component :is="Component" :key="route.fullPath" />
        </router-view>
      </main>
    </div>
    <AgentChatDrawer v-model:visible="agentVisible" />
  </div>
</template>

<style scoped lang="scss">
.app-layout {
  display: flex;
  width: 100%;
  height: 100%;
  background: var(--color-bg-page);
}

.app-layout-right {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
}

.app-layout-main {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
  background: var(--color-bg-page);
}

.sidebar-mask {
  display: none;
}

.page-fade-enter-active,
.page-fade-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}

.page-fade-enter-from {
  opacity: 0;
  transform: translateY(4px);
}

.page-fade-leave-to {
  opacity: 0;
}

.sidebar-mask-fade-enter-active,
.sidebar-mask-fade-leave-active {
  transition: opacity 0.18s ease;
}

.sidebar-mask-fade-enter-from,
.sidebar-mask-fade-leave-to {
  opacity: 0;
}

@media (max-width: 768px) {
  .app-layout {
    overflow-x: hidden;
  }

  .app-layout-main {
    padding: 16px 12px;
  }

  .sidebar-mask {
    display: block;
    position: fixed;
    inset: 0;
    z-index: 1990;
    background: rgba(13, 18, 32, 0.42);
  }
}
</style>
