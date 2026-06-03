<script setup lang="ts">
import { computed } from 'vue'
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
  Avatar,
  User,
  ChatDotRound,
  Lock,
  Aim,
  MagicStick,
} from '@element-plus/icons-vue'
import { useUserStore } from '@/stores/user'

interface MenuItem {
  path: string
  title: string
  icon: typeof HomeFilled
  admin?: boolean
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
  { path: '/dashboard', title: '工作台',     icon: HomeFilled },
  { path: '/projects',  title: '项目管理',   icon: FolderOpened },
  { path: '/code',      title: '代码中心',   icon: Document },
  { path: '/reviews',   title: '审查任务',   icon: DocumentChecked },
  { path: '/issues',    title: '问题追踪',   icon: Warning },
  { path: '/reports',   title: '审查报告',   icon: DataBoard },
  { path: '/agents',    title: 'Agent 中心', icon: Cpu },
  { path: '/security',  title: '安全中心',   icon: Aim },
  { path: '/rules',     title: '审查规则',   icon: List },
  { path: '/profile',   title: '个人中心',   icon: Avatar },
]

const adminItems: MenuItem[] = [
  { path: '/admin/users',    title: '用户管理',        icon: User,          admin: true },
  { path: '/admin/ai-logs',  title: 'Agent 调用日志', icon: ChatDotRound,  admin: true },
  { path: '/admin/audit',    title: '系统操作审计',    icon: Lock,          admin: true },
  { path: '/admin/evolution', title: 'Agent 自进化',   icon: MagicStick,    admin: true },
]

const isAdmin = computed(() => userStore.profile?.role === 'admin')

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
  <aside class="app-sidebar" :class="{ 'is-mobile-open': props.mobileOpen }">
    <div class="sidebar-logo">
      <span class="prism-mark sm"></span>
      <div class="logo-meta">
        <span class="logo-text font-display">Prism</span>
        <span class="logo-sub font-mono">CODE REVIEW</span>
      </div>
    </div>

    <nav class="sidebar-nav">
      <div class="nav-group">
        <div class="nav-group-label font-mono">主导航</div>
        <button
          v-for="item in menuItems"
          :key="item.path"
          class="nav-item"
          :class="{ 'is-active': isActive(item.path) }"
          @click="go(item)"
        >
          <el-icon class="nav-icon"><component :is="item.icon" /></el-icon>
          <span class="nav-text">{{ item.title }}</span>
        </button>
      </div>

      <div v-if="isAdmin" class="nav-group">
        <div class="nav-group-label font-mono">管理</div>
        <button
          v-for="item in adminItems"
          :key="item.path"
          class="nav-item"
          :class="{ 'is-active': isActive(item.path) }"
          @click="go(item)"
        >
          <el-icon class="nav-icon"><component :is="item.icon" /></el-icon>
          <span class="nav-text">{{ item.title }}</span>
        </button>
      </div>
    </nav>

    <div class="sidebar-foot">
      <div class="version font-mono">v1.0 · PRISM</div>
    </div>
  </aside>
</template>

<style scoped lang="scss">
.app-sidebar {
  width: var(--sidebar-width);
  height: 100%;
  background: var(--side-bg);
  border-right: 1px solid var(--side-border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  flex-shrink: 0;
  position: relative;
  z-index: 2000;
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

/* 导航 ----------------------------------- */
.sidebar-nav {
  flex: 1;
  overflow-y: auto;
  padding: 12px 0;
}

.nav-group + .nav-group {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--side-border);
}

.nav-group-label {
  padding: 6px 24px;
  font-size: 10px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--side-text-dim);
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
      left: -8px;
      top: 50%;
      transform: translateY(-50%);
      width: 3px;
      height: 18px;
      background: var(--brand-300);
      border-radius: 0 2px 2px 0;
    }
  }
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
  padding: 16px 24px;
  border-top: 1px solid var(--side-border);
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
    max-width: 320px;
    transform: translateX(-100%);
    transition: transform 0.22s ease;
    box-shadow: 12px 0 28px rgba(0, 0, 0, 0.24);
  }

  .app-sidebar.is-mobile-open {
    transform: translateX(0);
  }
}
</style>
