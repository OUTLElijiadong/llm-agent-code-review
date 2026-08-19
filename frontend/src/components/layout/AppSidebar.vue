<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  HomeFilled,
  FolderOpened,
  Document,
  DocumentChecked,
  Warning,
  List,
  DataBoard,
  Cpu,
  EditPen,
  Avatar,
  User,
  ChatDotRound,
  Lock,
  Aim,
  MagicStick,
  ChatLineSquare,
  Collection,
  Star,
  Tools,
  Comment,
  Connection,
  Operation,
  Key,
  Stamp,
  UserFilled,
  Monitor,
  Fold,
  Expand,
  ArrowDown,
} from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'
import { APP_DISPLAY_VERSION } from '@/constants/buildInfo'
import {
  canRoleSeeNavigationItem,
  normalizeRole,
  type UserRole,
} from '@/utils/roleHome'

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

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

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
  { path: '/issues',    title: '问题追踪',   icon: Warning,          roles: ['user', 'reviewer'] },
  { path: '/reports',   title: '审查报告',   icon: DataBoard,        roles: ['user', 'reviewer'] },
  { path: '/agents',    title: 'Agent 中心', icon: Cpu,              roles: ['admin', 'user', 'reviewer'] },
  { path: '/sandboxes', title: '代码沙箱',   icon: Monitor,          roles: ['admin', 'user', 'reviewer'] },
  { path: '/agent-studio', title: 'Agent 工坊', icon: EditPen,       roles: ['admin', 'user', 'reviewer'] },
  { path: '/security',  title: '安全中心',   icon: Aim,              roles: ['admin', 'user', 'reviewer'] },
  { path: '/rules',     title: '审查规则',   icon: List,             roles: ['user', 'reviewer'] },
  { path: '/forum',     title: '开发者论坛', icon: ChatLineSquare,   roles: ['admin', 'user', 'reviewer'] },
  { path: '/knowledge', title: '个人知识库', icon: Collection,       roles: ['user', 'reviewer'] },
  { path: '/profile/personalization', title: '个性化画像', icon: Star, roles: ['user', 'reviewer'] },
  { path: '/support/maintenance', title: '申请维修', icon: Tools,    roles: ['admin', 'user', 'reviewer'] },
  { path: '/support/feedback',    title: '意见反馈', icon: Comment,  roles: ['admin', 'user', 'reviewer'] },
  { path: '/profile',   title: '个人中心',   icon: Avatar,           roles: ['admin', 'user', 'reviewer'] },
]

const adminItems: MenuItem[] = [
  { path: '/admin/users',          title: '用户管理',        icon: User,          admin: true },
  { path: '/admin/rbac/roles',     title: '角色管理',        icon: Key,           admin: true },
  { path: '/admin/rbac/permissions', title: '权限点列表',    icon: Stamp,         admin: true },
  { path: '/admin/rbac/users',     title: '用户角色分配',    icon: UserFilled,    admin: true },
  { path: '/admin/ai-logs',        title: 'Agent 调用日志', icon: ChatDotRound,  admin: true },
  { path: '/admin/audit',          title: '系统操作审计',    icon: Lock,          admin: true },
  { path: '/admin/evolution',      title: 'Agent 自进化',    icon: MagicStick,    admin: true },
  { path: '/admin/llm',            title: '大模型配置',      icon: Operation,     admin: true, superAdmin: true },
  { path: '/admin/embedding',      title: 'RAG 嵌入配置',   icon: Connection,    admin: true, superAdmin: true },
  { path: '/admin/mcp-workers',    title: 'MCP 与沙箱节点', icon: Monitor,       admin: true, superAdmin: true },
]

const isAdmin = computed(() => userStore.isAdmin())
const currentRole = computed(() => normalizeRole(userStore.profile?.role))

const visibleMenuItems = computed(() => {
  // 管理员只做管理内容工作:不显示用户端功能菜单(工作台/代码沙箱/论坛等),
  // 侧边栏仅保留"管理"菜单;普通用户按 roles 显示。
  if (isAdmin.value) return []
  // 主菜单统一按静态 roles 分角色显示。后端 RBAC 菜单种子的 path 与前端路由
  // 不一致(如 /review vs /reviews、缺 /forum),做交集会误删论坛等入口;
  // 数据权限仍由后端强制。
  return menuItems.filter((item) => (
    canRoleSeeNavigationItem(currentRole.value, item.roles)
  ))
})

const visibleAdminItems = computed(() => (
  adminItems.filter((item) => !item.superAdmin || userStore.isSuperAdmin())
))

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
  { key: 'review', title: '智能审查', paths: ['/reviews', '/issues', '/reports', '/rules'] },
  { key: 'agents', title: 'Agent 与安全', paths: ['/agents', '/sandboxes', '/agent-studio', '/security'] },
  { key: 'community', title: '社区与支持', paths: ['/forum', '/support/maintenance', '/support/feedback'] },
  { key: 'personal', title: '个人空间', paths: ['/knowledge', '/profile/personalization', '/profile'] },
]

const navigationGroups = computed<MenuGroup[]>(() => {
  if (isAdmin.value) {
    return [{ key: 'admin', title: '系统管理', items: visibleAdminItems.value }]
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
  router.push(item.path)
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
          @click="toggleCollapsed"
        >
          <el-icon><component :is="isCollapsed ? Expand : Fold" /></el-icon>
        </button>
      </el-tooltip>
    </div>

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

        <div v-show="isCollapsed || isGroupExpanded(group.key)" class="nav-group-items">
          <el-tooltip
            v-for="item in group.items"
            :key="item.path"
            :content="item.title"
            placement="right"
            :disabled="!isCollapsed"
          >
            <button
              class="nav-item"
              :class="{ 'is-active': isActive(item.path) }"
              type="button"
              :aria-label="item.title"
              :aria-current="isActive(item.path) ? 'page' : undefined"
              @click="go(item)"
            >
              <el-icon class="nav-icon"><component :is="item.icon" /></el-icon>
              <span v-show="!isCollapsed" class="nav-text">{{ item.title }}</span>
            </button>
          </el-tooltip>
        </div>
      </section>
    </nav>

    <div class="sidebar-foot">
      <div class="version font-mono" :title="APP_DISPLAY_VERSION">
        {{ isCollapsed ? APP_DISPLAY_VERSION : `${APP_DISPLAY_VERSION} · PRISM` }}
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
  z-index: 2000;
  transition: width 0.2s ease, box-shadow 0.2s ease;

  &.is-collapsed {
    width: 72px;
  }
}

/* 顶部品牌区 ----------------------------- */
.sidebar-logo {
  display: flex;
  align-items: center;
  gap: 12px;
  height: var(--header-height);
  padding: 0 20px;
  border-bottom: 1px solid var(--side-border);
  flex-shrink: 0;
}

.is-collapsed .sidebar-logo {
  justify-content: center;
  padding: 0 10px;
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
  margin-left: auto;
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

.is-collapsed .sidebar-toggle {
  position: absolute;
  right: -13px;
  top: 17px;
  background: var(--side-bg);
  box-shadow: 0 8px 20px rgba(8, 12, 24, 0.24);
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
  width: 44px;
  height: 42px;
  margin: 2px auto;
  padding: 0;
  justify-content: center;
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
  font-size: 10px;
  color: var(--side-text-dim);
  letter-spacing: 0.1em;
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
    justify-content: flex-start;
    padding: 0 20px;
  }

  .app-sidebar.is-collapsed .sidebar-toggle {
    position: static;
    margin-left: auto;
  }
}
</style>
