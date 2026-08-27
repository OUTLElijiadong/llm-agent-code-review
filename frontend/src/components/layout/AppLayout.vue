<script setup lang="ts">
import { ref } from 'vue'
import AppSidebar from './AppSidebar.vue'
import AppHeader from './AppHeader.vue'
import ProactivePageGuide from '@/components/ai/ProactivePageGuide.vue'

const sidebarVisible = ref(false)

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
          <transition name="content-route" mode="out-in">
            <component :is="Component" :key="route.path" />
          </transition>
        </router-view>
      </main>
    </div>
    <ProactivePageGuide surface="user" />
  </div>
</template>

<style scoped lang="scss">
.app-layout {
  display: flex;
  width: 100%;
  height: 100%;
  min-height: 100dvh;
  overflow: hidden;
  background:
    linear-gradient(180deg, var(--app-bg-soft) 0%, var(--app-bg) 42%, #F1F4F9 100%);
}

.app-layout-right {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  min-height: 0;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.58), rgba(255, 255, 255, 0) 240px);
}

.app-layout-main {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  padding: var(--layout-main-padding);
  background: transparent;
  scroll-padding-top: var(--layout-main-padding);
}

.sidebar-mask {
  display: none;
}

.content-route-enter-active,
.content-route-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}

.content-route-enter-from {
  opacity: 0;
  transform: translateY(6px);
}

.content-route-leave-to {
  opacity: 0;
  transform: translateY(-2px);
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
    height: 100dvh;
  }

  .app-layout-main {
    padding: var(--layout-main-padding) 12px 24px;
  }

  .sidebar-mask {
    display: block;
    position: fixed;
    inset: 0;
    z-index: 1990;
    background: rgba(13, 18, 32, 0.42);
  }
}

@media (max-width: 420px) {
  .app-layout-main {
    padding: var(--layout-main-padding) 10px 22px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .content-route-enter-active,
  .content-route-leave-active {
    transition: none;
  }

  .content-route-enter-from,
  .content-route-leave-to {
    transform: none;
  }
}
</style>
