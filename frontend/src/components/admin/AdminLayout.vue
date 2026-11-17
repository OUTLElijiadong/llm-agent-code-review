<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowDown,
  Cpu,
  DataAnalysis,
  CreditCard,
  Lock,
  ChatLineSquare,
  Connection,
  Setting,
  Switch,
  SwitchButton,
  Tools,
  User,
} from '@element-plus/icons-vue'

import { useUserStore } from '@/stores/user'
import { useWorkspaceStore } from '@/stores/workspace'
import ProactivePageGuide from '@/components/ai/ProactivePageGuide.vue'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { isNavigationPathAllowed } from '@/utils/agentNavigation'
import UserAvatar from '@/components/common/UserAvatar.vue'
import { ADMIN_NAVIGATION_GROUPS, type AdminNavigationGroupKey } from '@/utils/adminNavigation'
import { isAdminSharedRoute } from '@/utils/adminSharedLayout'

interface AdminMenuItem {
  path: string
  title: string
  icon: typeof Cpu
}

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const workspaceStore = useWorkspaceStore()
const contentRef = ref<HTMLElement | null>(null)
const navigationRef = ref<HTMLElement | null>(null)
const navigationInteracted = ref(false)

const menuItems: AdminMenuItem[] = [
  { path: '/admin/governance', title: 'Agent 治理', icon: Cpu },
  { path: '/admin/operations', title: '运行与审计', icon: DataAnalysis },
  { path: '/admin/security-center', title: '安全中心', icon: Lock },
  { path: '/admin/access', title: '用户与权限', icon: User },
  { path: '/admin/platform', title: '平台配置', icon: Setting },
  { path: '/admin/enterprise-asset-publications', title: '企业 Agent 全局审批', icon: Cpu },
  { path: '/admin/billing-refunds', title: '计费与退款', icon: CreditCard },
  { path: '/admin/billing-statements', title: '渠道账单对账', icon: CreditCard },
  { path: '/admin/billing-reservations', title: '模型账本对账', icon: CreditCard },
  { path: '/admin/billing-reviews', title: '账本待复核', icon: CreditCard },
  { path: '/admin/billing-rate-reviews', title: '费率待复核', icon: CreditCard },
  { path: '/admin/billing-refund-reviews', title: '退款待审核', icon: CreditCard },
  { path: '/admin/billing-pricing', title: '模型费率目录', icon: CreditCard },
  { path: '/usage', title: '我的 Token 用量', icon: DataAnalysis },
  { path: '/billing', title: '充值与订阅', icon: CreditCard },
  { path: '/enterprise', title: '企业空间', icon: ChatLineSquare },
  { path: '/my-agents', title: '我的 Agent', icon: Cpu },
  { path: '/agent-assets', title: '共享 Agent 与 Skill', icon: Cpu },
  { path: '/remote-pentests', title: '远程网站评估', icon: Connection },
  { path: '/forum', title: '开发者论坛', icon: ChatLineSquare },
  { path: '/support', title: '支持中心', icon: Tools },
]

const visibleMenuItems = computed(() => (
  menuItems.filter((item) => (
    (item.path !== '/forum' || workspaceStore.hasEnterpriseMembership === false)
    && isNavigationPathAllowed(router, item.path, userStore)
  ))
))

const navigationGroups = computed(() => ADMIN_NAVIGATION_GROUPS.map((group) => ({
  ...group,
  items: group.paths
    .map((path) => visibleMenuItems.value.find((item) => item.path === path))
    .filter((item): item is AdminMenuItem => Boolean(item)),
})).filter((group) => group.items.length > 0))
const expandedGroups = ref<Partial<Record<AdminNavigationGroupKey, boolean>>>({})

watch(
  () => [userStore.profile?.id ?? null, userStore.token] as const,
  ([userId, token]) => {
    workspaceStore.syncIdentity(userId, token)
    if (userId && token) void workspaceStore.refreshEnterpriseMembership()
  },
  { immediate: true },
)

const activePath = computed(() => {
  const found = visibleMenuItems.value.find(
    (item) => route.path === item.path || route.path.startsWith(item.path + '/'),
  )
  return found?.path || '/admin/operations'
})

/**
 * 跳转到管理菜单对应页面。
 * @param path - 菜单路径。
 * @returns void
 */
function go(path: string): void {
  if (route.path === path) {
    contentRef.value?.scrollTo({ top: 0, behavior: 'smooth' })
    return
  }
  router.push(path)
}

function restorePageGuide(): void {
  window.dispatchEvent(new Event('prism:restore-page-guide'))
}

function toggleWorkspace(): void {
  if (workspaceStore.activeSpace === 'enterprise') {
    workspaceStore.activatePersonal()
  } else {
    workspaceStore.activateEnterprise()
    void router.push('/enterprise')
  }
}

function revealActiveNavigation(): void {
  if (window.innerWidth <= 920) {
    navigationRef.value?.querySelector<HTMLElement>('.admin-nav-item.is-active')
      ?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
  }
}

onMounted(() => { void nextTick(revealActiveNavigation) })

watch(() => workspaceStore.hasEnterpriseMembership, (membership, previous) => {
  if (previous === null && membership !== null && !navigationInteracted.value) {
    void nextTick(revealActiveNavigation)
  }
})

watch(
  () => route.fullPath,
  async (current, previous) => {
    const pathChanged = current.split(/[?#]/, 1)[0] !== previous.split(/[?#]/, 1)[0]
    if (pathChanged) navigationInteracted.value = false
    const activeGroup = navigationGroups.value.find((group) => group.items.some((item) => (
      route.path === item.path || route.path.startsWith(`${item.path}/`)
    )))
    if (activeGroup) expandedGroups.value = { ...expandedGroups.value, [activeGroup.key]: true }
    await nextTick()
    contentRef.value?.scrollTo?.({ top: 0, behavior: 'auto' })
    if (pathChanged) revealActiveNavigation()
  },
)

function isGroupExpanded(key: AdminNavigationGroupKey): boolean {
  if (Object.prototype.hasOwnProperty.call(expandedGroups.value, key)) return expandedGroups.value[key] === true
  const group = navigationGroups.value.find((item) => item.key === key)
  return Boolean(group?.items.some((item) => route.path === item.path || route.path.startsWith(`${item.path}/`)))
}

function toggleGroup(key: AdminNavigationGroupKey): void {
  expandedGroups.value = { ...expandedGroups.value, [key]: !isGroupExpanded(key) }
}

/**
 * 退出当前账号并回到登录页。
 * @returns Promise<void>
 */
async function logout(): Promise<void> {
  try {
    await ElMessageBox.confirm('确定要退出管理后台吗？', '提示', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    })
    await userStore.logout()
    router.push('/login')
  } catch {
    /* 用户取消 */
  }
}
</script>

<template>
  <div class="admin-layout">
    <aside class="admin-sidebar">
      <div class="admin-brand">
        <span class="prism-mark"></span>
        <div>
          <div class="admin-logo font-display">Prism</div>
          <div class="admin-sub font-mono">平台管理</div>
        </div>
      </div>
      <button
        type="button"
        class="admin-workspace-switch"
        data-workspace-switch
        :aria-label="workspaceStore.activeSpace === 'enterprise' ? '切换到个人空间' : '切换到企业空间'"
        @click="toggleWorkspace"
      >
        <el-icon><Switch /></el-icon>
        <span>{{ workspaceStore.activeSpace === 'enterprise' ? '企业空间' : '个人空间' }}</span>
      </button>
      <nav
        ref="navigationRef"
        class="admin-nav"
        aria-label="管理与社区导航"
        @pointerdown="navigationInteracted = true"
        @touchstart.passive="navigationInteracted = true"
        @wheel.passive="navigationInteracted = true"
        @keydown="navigationInteracted = true"
      >
        <section v-for="group in navigationGroups" :key="group.key" class="admin-nav-group">
          <button
            type="button"
            class="admin-nav-section"
            :aria-expanded="isGroupExpanded(group.key)"
            @click="toggleGroup(group.key)"
          >
            <span>{{ group.title }}</span>
            <el-icon :class="{ 'is-folded': !isGroupExpanded(group.key) }"><ArrowDown /></el-icon>
          </button>
          <div v-show="isGroupExpanded(group.key)" class="admin-nav-items">
            <button
              v-for="item in group.items"
              :key="item.path"
              type="button"
              class="admin-nav-item"
              :class="{ 'is-active': activePath === item.path }"
              :data-route="item.path"
              :aria-current="activePath === item.path ? 'page' : undefined"
              @click="go(item.path)"
            >
              <el-icon><component :is="item.icon" /></el-icon>
              <span>{{ item.title }}</span>
            </button>
          </div>
        </section>
      </nav>
    </aside>

    <section class="admin-main">
      <header class="admin-header">
        <div>
          <div class="admin-kicker font-mono">管理控制台</div>
          <h1>{{ route.meta.title || '管理后台' }}</h1>
        </div>
        <div class="admin-user">
          <el-dropdown trigger="click">
            <button type="button" class="admin-user-trigger" aria-label="打开账号菜单">
              <UserAvatar :avatar="userStore.profile?.avatar" :name="userStore.displayName" :user-id="userStore.profile?.id || 0" :size="34" />
              <span class="admin-user-meta">
                <span class="admin-user-name">{{ userStore.displayName || '管理员' }}</span>
                <span class="admin-user-role">{{ userStore.isSuperAdmin() ? '超级管理员' : '管理员' }}</span>
              </span>
              <el-icon><ArrowDown /></el-icon>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="restorePageGuide">重新显示页面引导</el-dropdown-item>
                <el-dropdown-item @click="router.push('/profile/password')">修改密码</el-dropdown-item>
                <el-dropdown-item divided @click="logout">
                  <span class="admin-logout"><el-icon><SwitchButton /></el-icon>退出登录</span>
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <div
            id="admin-copilot-trigger-slot"
            class="admin-copilot-trigger-slot"
            :class="{ 'is-page-header-trigger': route.path.startsWith('/admin') || isAdminSharedRoute(route.path) }"
            aria-label="小菱入口"
          ></div>
        </div>
      </header>

      <main ref="contentRef" class="admin-content">
        <ProactivePageGuide surface="admin" />
        <router-view v-slot="{ Component, route: childRoute }">
          <transition name="admin-route" mode="out-in">
            <component :is="Component" :key="childRoute.path" />
          </transition>
        </router-view>
      </main>
    </section>
  </div>
</template>

<style scoped lang="scss">
.admin-layout {
  display: flex;
  height: 100dvh;
  min-height: 100dvh;
  width: 100%;
  overflow: hidden;
  background: #F5F7FB;
  color: var(--gray-900);
}

.admin-sidebar {
  width: 252px;
  flex: 0 0 252px;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  background: var(--side-bg);
  border-right: 1px solid var(--side-border);
}

.admin-brand {
  height: 72px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 20px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.admin-workspace-switch {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 12px 10px 0;
  padding: 0 12px;
  min-height: 44px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 8px;
  color: rgba(255, 255, 255, 0.8);
  background: rgba(255, 255, 255, 0.05);
  cursor: pointer;
}

.admin-content :deep(.support-center .support-header) {
  display: none;
}

.admin-mark {
  width: 34px;
  height: 34px;
  border-radius: 8px;
  background: linear-gradient(135deg, #5B58E8, #25A5C4);
  box-shadow: 0 10px 24px rgba(91, 88, 232, 0.28);
}

.admin-logo {
  font-size: 17px;
  font-weight: 650;
  color: #fff;
  line-height: 1;
}

.admin-sub {
  margin-top: 4px;
  font-size: 10px;
  color: rgba(255, 255, 255, 0.46);
}

.admin-nav {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
  padding: 12px 10px 18px;
}

.admin-nav-item {
  position: relative;
  width: 100%;
  min-height: 44px;
  border: 0;
  border-radius: 8px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 12px;
  margin-bottom: 4px;
  color: rgba(255, 255, 255, 0.68);
  background: transparent;
  cursor: pointer;
  text-align: left;
  transition: color var(--transition-fast), background-color var(--transition-fast), transform var(--transition-fast);
}

.admin-nav-section {
  width: 100%;
  min-height: 36px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin: 8px 0 4px;
  padding: 0 12px;
  border: 0;
  border-radius: 7px;
  color: rgba(255, 255, 255, 0.44);
  background: transparent;
  font-size: 11px;
  line-height: 16px;
  white-space: nowrap;
  text-align: left;
  cursor: pointer;
}

.admin-nav-section:hover,
.admin-nav-section[aria-expanded="true"] {
  color: rgba(255, 255, 255, 0.74);
  background: rgba(255, 255, 255, 0.045);
}

.admin-nav-section :deep(.el-icon) {
  transition: transform var(--transition-fast);
}

.admin-nav-section :deep(.el-icon.is-folded) {
  transform: rotate(-90deg);
}

.admin-nav-item::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  width: 3px;
  height: 0;
  border-radius: 0 3px 3px 0;
  background: linear-gradient(180deg, var(--brand-300), var(--accent-400));
  transform: translateY(-50%);
  transition: height 0.2s ease;
}

.admin-nav-item:hover,
.admin-nav-item.is-active {
  color: #fff;
  background: rgba(91, 88, 232, 0.22);
}

.admin-nav-item:hover {
  transform: translateX(2px);
}

.admin-nav-item.is-active::before {
  height: 22px;
}

.admin-main {
  flex: 1;
  min-width: 0;
  min-height: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.admin-header {
  position: relative;
  z-index: var(--layer-header);
  flex: 0 0 auto;
  min-height: 72px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 16px 28px;
  background: var(--surface-glass);
  background: color-mix(in srgb, var(--surface-glass) 94%, transparent);
  border-bottom: 1px solid rgba(224, 227, 234, 0.88);
  box-shadow: 0 1px 0 rgba(255, 255, 255, 0.72) inset;
  backdrop-filter: blur(18px) saturate(1.1);
  -webkit-backdrop-filter: blur(18px) saturate(1.1);
}

.admin-kicker {
  font-size: 11px;
  color: var(--gray-500);
}

.admin-header h1 {
  margin: 3px 0 0;
  font-size: 22px;
  line-height: 1.2;
  letter-spacing: 0;
}

.admin-user {
  display: flex;
  align-items: center;
  gap: 12px;
  color: var(--gray-600);
}

.admin-user-trigger {
  display: flex;
  align-items: center;
  gap: 9px;
  min-height: 42px;
  padding: 3px 8px 3px 4px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  color: inherit;
  cursor: pointer;
  text-align: left;
}

.admin-user-trigger:hover,
.admin-user-trigger:focus-visible {
  border-color: var(--brand-200);
  background: var(--brand-50);
  outline: none;
}

.admin-user-meta { display: grid; gap: 2px; }
.admin-user-name { max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
.admin-user-role { color: var(--gray-500); font-size: 11px; }
.admin-logout { display: inline-flex; align-items: center; gap: 7px; }

.admin-copilot-trigger-slot {
  display: none;
  place-items: center;
  flex: 0 0 44px;
  min-width: 44px;
  min-height: 44px;
}

.admin-copilot-trigger-slot.is-page-header-trigger { display: grid; }

.admin-content {
  // 60px 副驾入口 + 24px 底距 + 16px 间隔，让页尾操作可滚动至入口上方。
  --assistant-action-clearance: calc(100px + env(safe-area-inset-bottom, 0px));
  flex: 1;
  min-width: 0;
  min-height: 0;
  padding: 24px 28px 36px;
  padding-bottom: max(36px, var(--assistant-action-clearance));
  // 宽表格在窄视口可横向滚动，不裁掉最右侧操作列。
  overflow-x: auto;
  overflow-y: auto;
  overscroll-behavior: contain;
  overflow-anchor: none;
  scrollbar-gutter: stable;
  scroll-padding-bottom: var(--assistant-action-clearance);
}

.admin-route-enter-active,
.admin-route-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}

.admin-route-enter-from {
  opacity: 0;
  transform: translateY(6px);
}

.admin-route-leave-to {
  opacity: 0;
  transform: translateY(-2px);
}

@media (max-width: 920px) {
  .admin-layout {
    flex-direction: column;
    height: 100dvh;
  }

  .admin-sidebar {
    width: 100%;
    max-width: 100%;
    height: auto;
    min-height: auto;
    flex-basis: auto;
    flex-shrink: 0;
  }

  .admin-nav {
    display: flex;
    flex: 0 0 auto;
    gap: 6px;
    overflow-x: auto;
    overflow-y: hidden;
    scrollbar-gutter: auto;
    padding: 10px;
  }

  .admin-nav-item {
    width: auto;
    white-space: nowrap;
    margin-bottom: 0;
  }

  .admin-nav-section {
    flex: 0 0 auto;
    min-height: 40px;
    width: auto;
    margin: 0 4px;
    padding: 0 10px;
  }

  .admin-nav-group {
    flex: 0 0 auto;
  }

  .admin-nav-items {
    display: flex;
    flex: 0 0 auto;
    gap: 6px;
  }

  .admin-content {
    padding-top: 16px;
    padding-inline: 16px;
  }

  .admin-header {
    align-items: flex-start;
    flex-wrap: wrap;
    padding: 14px 16px;
  }

  .admin-header > div:first-child {
    min-width: 0;
    flex: 1 1 220px;
  }

  .admin-user {
    flex: 1 1 100%;
    min-width: 0;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
  }
}

@media (max-width: 520px) {
  .admin-copilot-trigger-slot {
    display: grid;
  }
}

@media (max-width: 480px) {
  .admin-brand {
    height: 64px;
    padding: 0 14px;
  }

  .admin-logo {
    font-size: 16px;
  }

  .admin-nav {
    gap: 4px;
    padding: 8px;
  }

  .admin-nav-item {
    min-height: 38px;
    padding: 0 10px;
    font-size: 14px;
  }

  .admin-content {
    padding-top: 12px;
    padding-inline: 10px;
  }

  .admin-header h1 {
    font-size: 20px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .admin-nav-item,
  .admin-nav-item::before,
  .admin-route-enter-active,
  .admin-route-leave-active {
    transition: none;
  }

  .admin-nav-item:hover,
  .admin-route-enter-from,
  .admin-route-leave-to {
    transform: none;
  }
}
</style>
