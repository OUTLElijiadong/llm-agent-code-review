<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  HomeFilled,
  FolderOpened,
  Document,
  DocumentChecked,
  Warning,
  DataBoard,
  CreditCard,
  Cpu,
  Avatar,
  User,
  Aim,
  ChatLineSquare,
  Collection,
  Tools,
  Operation,
  Monitor,
  Lock,
  Fold,
  Expand,
  ArrowDown,
  Close,
  Connection,
  Switch,
} from '@element-plus/icons-vue'
import { usePendingRemoteApprovals } from '@/composables/usePendingRemoteApprovals'
import { useUserStore } from '@/stores/user'
import { useWorkspaceStore } from '@/stores/workspace'
import { APP_BUILD_TIME_DISPLAY, APP_DISPLAY_VERSION, APP_RELEASE_SHA } from '@/constants/buildInfo'
import type { UserRole } from '@/utils/roleHome'
import { isNavigationPathAllowed } from '@/utils/agentNavigation'

interface MenuItem {
  path: string
  title: string
  icon: typeof HomeFilled
  admin?: boolean
  superAdmin?: boolean
  roles?: UserRole[]
}

interface MenuGroup {
  key: string
  title: string
  items: MenuItem[]
}

const REMOTE_PENTEST_PATH = '/remote-pentests'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const workspaceStore = useWorkspaceStore()

const props = withDefaults(defineProps<{
  mobileOpen?: boolean
}>(), {
  mobileOpen: false,
})

const emit = defineEmits<{
  (e: 'close'): void
}>()

const menuItems: MenuItem[] = [
  { path: '/dashboard', title: '工作台',     icon: HomeFilled,       roles: ['admin', 'user', 'reviewer'] },
  { path: '/projects',  title: '项目管理',   icon: FolderOpened,     roles: ['user'] },
  { path: '/code',      title: '代码中心',   icon: Document,         roles: ['user'] },
  { path: '/reviews',   title: '审查任务',   icon: DocumentChecked,  roles: ['user', 'reviewer'] },
  { path: '/pentests',  title: '渗透测试',   icon: Aim,              roles: ['user', 'reviewer'] },
  { path: REMOTE_PENTEST_PATH, title: '远程网站评估', icon: Connection, roles: ['admin', 'user', 'reviewer'] },
  { path: '/issues',    title: '问题追踪',   icon: Warning,          roles: ['user', 'reviewer'] },
  { path: '/reports',   title: '审查报告',   icon: DataBoard,        roles: ['user', 'reviewer'] },
  { path: '/audit',     title: '操作审计',   icon: Operation,        roles: ['reviewer'] },
  { path: '/agents',    title: 'Agent 工作台', icon: Cpu,            roles: ['admin', 'user', 'reviewer'] },
  { path: '/my-agents', title: '我的 Agent', icon: Cpu,               roles: ['admin', 'user', 'reviewer'] },
  { path: '/sandboxes', title: '代码沙箱',   icon: Monitor,          roles: ['admin', 'user', 'reviewer'] },
  { path: '/security',  title: '安全与规则', icon: Aim,              roles: ['admin', 'user', 'reviewer'] },
  { path: '/forum',     title: '开发者论坛', icon: ChatLineSquare,   roles: ['admin', 'user', 'reviewer'] },
  { path: '/knowledge', title: '个人知识库', icon: Collection,       roles: ['user', 'reviewer'] },
  { path: '/usage',     title: '我的 Token 用量', icon: DataBoard,     roles: ['admin', 'user', 'reviewer'] },
  { path: '/billing',   title: '充值与订阅', icon: CreditCard, roles: ['admin', 'user', 'reviewer'] },
  { path: '/enterprise', title: '企业空间', icon: ChatLineSquare,      roles: ['admin', 'user', 'reviewer'] },
  { path: '/agent-assets', title: '共享 Agent 与 Skill', icon: Cpu, roles: ['admin', 'user', 'reviewer'] },
  { path: '/support',      title: '支持中心', icon: Tools,             roles: ['admin', 'user', 'reviewer'] },
  { path: '/profile',   title: '个人中心',   icon: Avatar,           roles: ['admin', 'user', 'reviewer'] },
]

const adminItems: MenuItem[] = [
  { path: '/admin/governance', title: 'Agent 治理', icon: Cpu, admin: true },
  { path: '/admin/operations', title: '运行与审计', icon: Operation, admin: true },
  { path: '/admin/security-center', title: '安全中心', icon: Lock, admin: true, superAdmin: true },
  { path: '/admin/billing-refunds', title: '计费与退款', icon: CreditCard, admin: true, superAdmin: true },
  { path: '/admin/billing-statements', title: '渠道账单对账', icon: CreditCard, admin: true, superAdmin: true },
  { path: '/admin/billing-reservations', title: '模型账本对账', icon: CreditCard, admin: true, superAdmin: true },
  { path: '/admin/billing-reviews', title: '账本待复核', icon: CreditCard, admin: true },
  { path: '/admin/billing-rate-reviews', title: '费率待复核', icon: CreditCard, admin: true },
  { path: '/admin/billing-refund-reviews', title: '退款待审核', icon: CreditCard, admin: true },
  { path: '/admin/billing-pricing', title: '模型费率目录', icon: CreditCard, admin: true, superAdmin: true },
  { path: '/admin/enterprise-asset-publications', title: '企业 Agent 全局审批', icon: Cpu, admin: true, superAdmin: true },
  { path: '/admin/access', title: '用户与权限', icon: User, admin: true },
  { path: '/admin/platform', title: '平台配置', icon: Tools, admin: true },
  { path: '/usage', title: '我的 Token 用量', icon: DataBoard, admin: true },
  { path: '/billing', title: '充值与订阅', icon: CreditCard, admin: true },
  { path: '/enterprise', title: '企业空间', icon: ChatLineSquare, admin: true },
  { path: '/my-agents', title: '我的 Agent', icon: Cpu, admin: true },
  // 远程目标授权的审批入口：管理员也要能申请并批准（申请人与批准人仍须是不同账号）。
  { path: REMOTE_PENTEST_PATH, title: '远程网站评估', icon: Connection, admin: true },
  { path: '/forum', title: '开发者论坛', icon: ChatLineSquare, admin: true },
  { path: '/support', title: '支持中心与反馈', icon: Tools, admin: true },
]

const isAdmin = computed(() => userStore.isAdmin())
const workspaceLabel = computed(() => workspaceStore.activeSpace === 'enterprise' ? '企业空间' : '个人空间')
const workspaceTargetLabel = computed(() => workspaceStore.activeSpace === 'enterprise' ? '切换到个人空间' : '切换到企业空间')

watch(
  () => [userStore.profile?.id ?? null, userStore.token] as const,
  ([userId, token]) => {
    workspaceStore.syncIdentity(userId, token)
    if (route.meta?.workspace === 'enterprise' && workspaceStore.activeSpace !== 'enterprise') {
      void router.replace('/dashboard')
    }
  },
  { immediate: true },
)

function isVisibleInWorkspace(path: string): boolean {
  const target = router.resolve({ path })
  return target.meta.workspace !== 'personal' || workspaceStore.activeSpace === 'personal'
}

const visibleMenuItems = computed(() => {
  // 管理员使用专属管理菜单；论坛与支持中心仍是全角色共用的业务入口。
  if (isAdmin.value) return []
  // 可见性与路由守卫共用实时 RBAC 判定。不再叠加静态角色白名单,
  // 避免审查员被授予 project:view 后仍看不到项目入口。
  return menuItems.filter((item) => isVisibleInWorkspace(item.path)
    && isNavigationPathAllowed(router, item.path, userStore))
})

const visibleAdminItems = computed(() => (
  adminItems.filter((item) => (
    (!item.superAdmin || userStore.isSuperAdmin())
    && isVisibleInWorkspace(item.path)
    && isNavigationPathAllowed(router, item.path, userStore)
  ))
))

/** 待审批角标：入口可见且账号能批准时才统计，且不含自己提交的申请。 */
const remoteEntryVisible = computed(() => (
  (isAdmin.value ? visibleAdminItems.value : visibleMenuItems.value)
    .some((item) => item.path === REMOTE_PENTEST_PATH)
))
const { count: pendingRemoteApprovals } = usePendingRemoteApprovals({
  enabled: () => remoteEntryVisible.value && userStore.hasPermission('pentest:authorize'),
  currentUserId: () => userStore.profile?.id,
})

function badgeText(item: MenuItem): string {
  if (item.path !== REMOTE_PENTEST_PATH || pendingRemoteApprovals.value <= 0) return ''
  return pendingRemoteApprovals.value > 99 ? '99+' : String(pendingRemoteApprovals.value)
}

function itemTooltip(item: MenuItem): string {
  const badge = badgeText(item)
  return badge ? `${item.title} · ${badge} 个待审批` : item.title
}

const COLLAPSED_KEY = 'prism.sidebar.collapsed'
const GROUPS_KEY = 'prism.sidebar.groups'
const isCollapsed = ref(
  typeof window !== 'undefined' && window.localStorage.getItem(COLLAPSED_KEY) === '1',
)
const expandedGroups = ref<Record<string, boolean>>({})

if (typeof window !== 'undefined') {
  try {
    expandedGroups.value = JSON.parse(window.localStorage.getItem(GROUPS_KEY) || '{}')
  } catch {
    expandedGroups.value = {}
  }
}

const userGroupDefinitions = [
  { key: 'workspace', title: '工作区', paths: ['/dashboard', '/projects', '/code'] },
  {
    key: 'review',
    title: '智能审查与审计',
    paths: ['/reviews', '/pentests', REMOTE_PENTEST_PATH, '/issues', '/reports', '/audit'],
  },
  { key: 'agents', title: 'Agent 与安全', paths: ['/agents', '/my-agents', '/agent-assets', '/sandboxes', '/security'] },
  { key: 'community', title: '社区与支持', paths: ['/forum', '/enterprise', '/support'] },
  { key: 'personal', title: '个人空间', paths: ['/knowledge', '/usage', '/profile/personalization', '/profile'] },
]

const navigationGroups = computed<MenuGroup[]>(() => {
  if (isAdmin.value) {
    const communityPaths = new Set(['/forum', '/support'])
    return [
      {
        key: 'admin',
        title: '系统管理',
        items: visibleAdminItems.value.filter((item) => !communityPaths.has(item.path)),
      },
      {
        key: 'community',
        title: '社区与支持',
        items: visibleAdminItems.value.filter((item) => communityPaths.has(item.path)),
      },
    ].filter((group) => group.items.length > 0)
  }
  return userGroupDefinitions
    .map((group) => ({
      key: group.key,
      title: group.title,
      items: group.paths
        .map((path) => visibleMenuItems.value.find((item) => item.path === path))
        .filter((item): item is MenuItem => Boolean(item)),
    }))
    .filter((group) => group.items.length > 0)
})

watch(isCollapsed, (value) => {
  window.localStorage.setItem(COLLAPSED_KEY, value ? '1' : '0')
})

watch(expandedGroups, (value) => {
  window.localStorage.setItem(GROUPS_KEY, JSON.stringify(value))
}, { deep: true })

function isGroupExpanded(key: string): boolean {
  return expandedGroups.value[key] !== false
}

function toggleGroup(key: string): void {
  expandedGroups.value[key] = !isGroupExpanded(key)
}

function toggleCollapsed(): void {
  isCollapsed.value = !isCollapsed.value
}

/**
 * 判断菜单项是否匹配当前路由
 * @param path - 菜单路径
 * @returns 是否为当前激活菜单
 */
function isActive(path: string): boolean {
  return route.path === path || route.path.startsWith(path + '/')
}

/**
 * 跳转到菜单项对应页面，并关闭移动端抽屉
 * @param item - 菜单项
 * @returns void
 */
function go(item: MenuItem): void {
  if (item.path === '/enterprise') workspaceStore.activateEnterprise(workspaceStore.activeEnterpriseId)
  void router.push(item.path)
  emit('close')
}

function toggleWorkspace(): void {
  if (workspaceStore.activeSpace === 'enterprise') {
    workspaceStore.activatePersonal()
    if (route.meta?.workspace === 'enterprise') void router.push('/dashboard')
  } else {
    workspaceStore.activateEnterprise()
    void router.push('/enterprise')
  }
  emit('close')
}
</script>

<template>
  <aside
    class="app-sidebar"
    :class="{
      'is-mobile-open': props.mobileOpen,
      'is-collapsed': isCollapsed,
    }"
  >
    <div class="sidebar-logo">
      <span class="prism-mark sm"></span>
      <div v-show="!isCollapsed" class="logo-meta">
        <span class="logo-text font-display">Prism</span>
        <span class="logo-sub font-mono">CODE REVIEW</span>
      </div>
      <el-tooltip :content="isCollapsed ? '展开侧边栏' : '收起侧边栏'" placement="right">
        <button
          class="sidebar-toggle"
          type="button"
          :aria-label="isCollapsed ? '展开侧边栏' : '收起侧边栏'"
          :aria-expanded="!isCollapsed"
          @click="toggleCollapsed"
        >
          <el-icon><component :is="isCollapsed ? Expand : Fold" /></el-icon>
        </button>
      </el-tooltip>
      <button class="sidebar-mobile-close" type="button" aria-label="关闭侧边栏" @click="emit('close')">
        <el-icon><Close /></el-icon>
      </button>
    </div>

    <button
      class="workspace-switcher"
      type="button"
      :aria-label="`${workspaceTargetLabel}，当前为${workspaceLabel}`"
      :title="workspaceTargetLabel"
      @click="toggleWorkspace"
    >
      <el-icon><Switch /></el-icon>
      <span v-show="!isCollapsed" class="workspace-switcher-label">
        <strong>{{ workspaceLabel }}</strong>
        <small>{{ workspaceTargetLabel }}</small>
      </span>
    </button>

    <nav class="sidebar-nav" aria-label="主导航">
      <section v-for="group in navigationGroups" :key="group.key" class="nav-group">
        <button
          class="nav-group-toggle"
          type="button"
          :aria-expanded="isGroupExpanded(group.key)"
          @click="toggleGroup(group.key)"
        >
          <span>{{ group.title }}</span>
          <el-icon :class="{ 'is-folded': !isGroupExpanded(group.key) }"><ArrowDown /></el-icon>
        </button>
        <div v-if="isCollapsed" class="nav-group-divider" aria-hidden="true"></div>

        <div v-show="(isCollapsed && !props.mobileOpen) || isGroupExpanded(group.key)" class="nav-group-items">
          <el-tooltip
            v-for="item in group.items"
            :key="item.path"
            :content="itemTooltip(item)"
            placement="right"
            :disabled="!isCollapsed"
          >
            <button
              class="nav-item"
              :class="{ 'is-active': isActive(item.path) }"
              type="button"
              :aria-label="item.title"
              :data-route="item.path"
              :aria-current="isActive(item.path) ? 'page' : undefined"
              @click="go(item)"
            >
              <el-icon class="nav-icon"><component :is="item.icon" /></el-icon>
              <span v-show="!isCollapsed" class="nav-text">{{ item.title }}</span>
              <span
                v-if="badgeText(item)"
                class="nav-badge"
                role="status"
                :aria-label="`${badgeText(item)} 个远程授权申请待审批`"
              >{{ badgeText(item) }}</span>
            </button>
          </el-tooltip>
        </div>
      </section>
    </nav>

    <div class="sidebar-foot">
      <div class="version font-mono" :title="`${APP_DISPLAY_VERSION} · ${APP_BUILD_TIME_DISPLAY} · ${APP_RELEASE_SHA}`">
        {{ isCollapsed ? APP_DISPLAY_VERSION : `${APP_DISPLAY_VERSION} · PRISM` }}
        <small v-if="!isCollapsed" class="version-meta">构建 {{ APP_BUILD_TIME_DISPLAY }}</small>
      </div>
    </div>
  </aside>
</template>

<style scoped lang="scss">
.app-sidebar {
  width: var(--sidebar-width);
  height: calc(100% - 24px);
  margin: 12px 0 12px 12px;
  background: var(--side-bg);
  border: 1px solid var(--side-border);
  border-radius: 8px;
  box-shadow: 0 18px 40px rgba(8, 12, 24, 0.18);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  flex-shrink: 0;
  position: relative;
  z-index: var(--layer-sidebar);
  transition: width 0.2s ease, box-shadow 0.2s ease;

  &.is-collapsed {
    width: var(--sidebar-collapsed-width, 72px);
  }
}

/* 顶部品牌区 ----------------------------- */
.sidebar-logo {
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr) 30px;
  align-items: center;
  gap: 12px;
  height: var(--header-height);
  padding: 0 20px;
  border-bottom: 1px solid var(--side-border);
  flex-shrink: 0;
}

.workspace-switcher {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 10px;
  min-height: 48px;
  margin: 10px 12px 2px;
  padding: 7px 12px;
  color: var(--side-text, #d7e1f2);
  background: rgba(255, 255, 255, 0.055);
  border: 1px solid var(--side-border);
  border-radius: 7px;
  cursor: pointer;
  text-align: left;
  transition: background 0.16s ease, border-color 0.16s ease;

  &:hover,
  &:focus-visible {
    background: rgba(255, 255, 255, 0.11);
    border-color: rgba(135, 170, 225, 0.55);
    outline: none;
  }

  .el-icon { flex: 0 0 auto; font-size: 17px; }
}

.workspace-switcher-label {
  display: grid;
  gap: 2px;
  min-width: 0;

  strong { font-size: 12px; font-weight: 650; }
  small { color: var(--side-muted, #93a3bb); font-size: 10px; }
}

.is-collapsed .sidebar-logo {
  grid-template-columns: 1fr;
  justify-items: center;
  gap: 8px;
  height: auto;
  padding: 14px 10px 10px;
}

.is-collapsed .workspace-switcher {
  justify-content: center;
  width: 44px;
  height: 42px;
  min-height: 42px;
  margin: 8px auto 4px;
  padding: 0;
}

.logo-meta {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.logo-text {
  font-size: 17px;
  font-weight: 600;
  color: #fff;
  line-height: 1;
}

.logo-sub {
  font-size: 9.5px;
  letter-spacing: 0.14em;
  color: var(--side-text-dim);
}

.sidebar-toggle {
  width: 30px;
  height: 30px;
  padding: 0;
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.04);
  color: var(--side-text);
  cursor: pointer;
  display: grid;
  place-items: center;

  &:hover,
  &:focus-visible {
    color: #fff;
    border-color: rgba(255, 255, 255, 0.24);
    outline: none;
  }
}

.sidebar-mobile-close {
  display: none;
}

.is-collapsed .sidebar-toggle {
  width: 36px;
  height: 32px;
}

/* 导航 ----------------------------------- */
.sidebar-nav {
  flex: 1;
  overflow-y: auto;
  padding: 10px 0;
  scrollbar-width: thin;
}

.nav-group + .nav-group {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--side-border);
}

.nav-group-toggle {
  width: calc(100% - 16px);
  min-height: 32px;
  margin: 0 8px 2px;
  padding: 0 12px;
  border: 0;
  background: transparent;
  color: var(--side-text-dim);
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11px;
  font-family: inherit;
  cursor: pointer;

  .el-icon {
    transition: transform 0.18s ease;
  }

  .el-icon.is-folded {
    transform: rotate(-90deg);
  }

  &:hover,
  &:focus-visible {
    color: #fff;
    outline: none;
  }
}

.is-collapsed .nav-group-toggle {
  display: none;
}

.nav-group-divider {
  width: 24px;
  height: 1px;
  margin: 7px auto;
  background: var(--side-border);
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 12px;
  width: calc(100% - 16px);
  height: 40px;
  margin: 2px 8px;
  padding: 0 14px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--side-text);
  font-size: 13.5px;
  font-family: inherit;
  cursor: pointer;
  text-align: left;
  transition: background var(--transition-fast), color var(--transition-fast);

  &:hover {
    background: rgba(255, 255, 255, 0.04);
    color: #fff;
  }

  &.is-active {
    background: var(--side-active-bg);
    color: var(--side-active-fg);
    position: relative;

    &::before {
      content: '';
      position: absolute;
      left: 0;
      top: 50%;
      transform: translateY(-50%);
      width: 3px;
      height: 18px;
      background: var(--brand-300);
      border-radius: 0 2px 2px 0;
    }
  }
}

.is-collapsed .nav-item {
  position: relative;
  width: 44px;
  height: 42px;
  margin: 2px auto;
  padding: 0;
  justify-content: center;
}

/* 待审批角标：展开时贴在右侧，收起时缩成图标右上角的小圆点式数字 */
.nav-badge {
  flex-shrink: 0;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 9px;
  background: var(--brand-400);
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  line-height: 18px;
  text-align: center;
}

.is-collapsed .nav-badge {
  position: absolute;
  top: 3px;
  right: 3px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  font-size: 10px;
  line-height: 16px;
}

.nav-icon {
  font-size: 17px;
  flex-shrink: 0;
}

.nav-text {
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 底部 ----------------------------------- */
.sidebar-foot {
  padding: 14px 20px;
  border-top: 1px solid var(--side-border);
}

.is-collapsed .sidebar-foot {
  padding: 14px 4px;
  text-align: center;
}

.version {
  display: grid;
  gap: 4px;
  font-size: 10px;
  color: var(--side-text-dim);
  letter-spacing: 0.1em;
}

.version-meta {
  font-size: 8px;
  letter-spacing: 0;
  line-height: 1.3;
  opacity: 0.75;
}

@media (max-width: 768px) {
  .app-sidebar {
    position: fixed;
    left: 0;
    top: 0;
    width: min(var(--sidebar-width), 82vw);
    height: 100%;
    margin: 0;
    border-radius: 0;
    max-width: 320px;
    transform: translateX(-100%);
    transition: transform 0.22s ease;
    box-shadow: 12px 0 28px rgba(0, 0, 0, 0.24);
  }

  .app-sidebar.is-mobile-open {
    transform: translateX(0);
  }

  .sidebar-toggle {
    display: none;
  }

  .sidebar-logo {
    grid-template-columns: 22px minmax(0, 1fr) 40px;
  }

  .sidebar-mobile-close {
    display: grid;
    place-items: center;
    width: 40px;
    height: 40px;
    padding: 0;
    border: 0;
    border-radius: 8px;
    background: transparent;
    color: var(--side-text);
    cursor: pointer;
  }

  .nav-group-toggle {
    min-height: 40px;
  }

  .app-sidebar.is-collapsed {
    width: min(var(--sidebar-width), 82vw);
  }

  .app-sidebar.is-collapsed .logo-meta,
  .app-sidebar.is-collapsed .nav-text {
    display: flex !important;
  }

  .app-sidebar.is-collapsed .nav-group-toggle {
    display: flex;
  }

  .app-sidebar.is-collapsed .nav-group-divider {
    display: none;
  }

  .app-sidebar.is-collapsed .nav-item {
    width: calc(100% - 16px);
    justify-content: flex-start;
    padding: 0 14px;
  }

  .app-sidebar.is-collapsed .sidebar-logo {
    grid-template-columns: 22px minmax(0, 1fr) 40px;
    justify-items: start;
    gap: 12px;
    height: var(--header-height);
    padding: 0 20px;
  }
}
</style>
