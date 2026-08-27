<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Bell,
  Cpu,
  DataAnalysis,
  Document,
  Files,
  Histogram,
  Key,
  Lock,
  MagicStick,
  Operation,
  Refresh,
  Setting,
  SwitchButton,
  Timer,
  Tools,
  TrendCharts,
  User,
  View,
  Connection,
} from '@element-plus/icons-vue'

import { useUserStore } from '@/stores/user'
import AdminCopilot from '@/components/admin/AdminCopilot.vue'
import ProactivePageGuide from '@/components/ai/ProactivePageGuide.vue'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { isNavigationPathAllowed } from '@/utils/agentNavigation'

interface AdminMenuItem {
  path: string
  title: string
  icon: typeof Histogram
  superAdmin?: boolean
}

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()

const menuItems: AdminMenuItem[] = [
  { path: '/admin/overview', title: '总览大屏', icon: Histogram },
  { path: '/admin/agents', title: 'Agent 管理', icon: Cpu },
  { path: '/admin/approvals', title: '审批中心', icon: Lock },
  { path: '/admin/agent-releases', title: 'Agent 发布审批', icon: Cpu },
  { path: '/admin/beta-codes', title: '内测码管理', icon: Key },
  { path: '/admin/policies', title: '策略中心', icon: Operation },
  { path: '/admin/tools', title: '工具权限', icon: Tools },
  { path: '/admin/knowledge', title: '知识与记忆', icon: Files },
  { path: '/admin/jobs', title: '任务调度', icon: Timer },
  { path: '/admin/observability', title: '监控告警', icon: DataAnalysis },
  { path: '/admin/rewards', title: '奖惩趋势', icon: TrendCharts },
  { path: '/admin/rollback', title: '回滚中心', icon: Refresh },
  { path: '/admin/users', title: '用户管理', icon: User },
  { path: '/admin/rbac/roles', title: '角色管理', icon: Lock },
  { path: '/admin/rbac/permissions', title: '权限点列表', icon: Key },
  { path: '/admin/rbac/users', title: '用户角色分配', icon: User },
  { path: '/admin/ai-logs', title: 'Agent 调用日志', icon: MagicStick },
  { path: '/admin/report-templates', title: '报告模板', icon: Document },
  { path: '/admin/audit', title: '系统操作审计', icon: Bell },
  { path: '/admin/evolution', title: 'Agent 自进化', icon: MagicStick },
  { path: '/admin/skills', title: 'Skill 管理', icon: View },
  { path: '/admin/mcp-workers', title: 'MCP 与沙箱节点', icon: Connection, superAdmin: true },
  { path: '/admin/llm', title: '大模型配置', icon: Setting, superAdmin: true },
  { path: '/admin/embedding', title: 'RAG 嵌入配置', icon: Key, superAdmin: true },
]

const visibleMenuItems = computed(() => (
  menuItems.filter((item) => (
    (!item.superAdmin || userStore.isSuperAdmin())
    && isNavigationPathAllowed(router, item.path, userStore)
  ))
))

const activePath = computed(() => {
  const found = visibleMenuItems.value.find(
    (item) => route.path === item.path || route.path.startsWith(item.path + '/'),
  )
  return found?.path || '/admin/overview'
})

/**
 * 跳转到管理菜单对应页面。
 * @param path - 菜单路径。
 * @returns void
 */
function go(path: string): void {
  if (route.path !== path) router.push(path)
}

/**
 * 退出当前账号并回到登录页。
 * @returns Promise<void>
 */
async function logout(): Promise<void> {
  try {
    await ElMessageBox.confirm('确定要退出管理后台吗？', '提示', {
      confirmButtonText: '退出',
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
          <div class="admin-logo font-display">Prism 管理后台</div>
          <div class="admin-sub font-mono">智能体治理</div>
        </div>
      </div>
      <nav class="admin-nav">
        <button
          v-for="item in visibleMenuItems"
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
      </nav>
    </aside>

    <section class="admin-main">
      <header class="admin-header">
        <div>
          <div class="admin-kicker font-mono">管理控制台</div>
          <h1>{{ route.meta.title || '管理后台' }}</h1>
        </div>
        <div class="admin-user">
          <span>{{ userStore.displayName || '管理员' }}</span>
          <el-tag size="small" :type="userStore.isSuperAdmin() ? 'danger' : 'warning'">
            {{ userStore.isSuperAdmin() ? '超级管理员' : '管理员' }}
          </el-tag>
          <el-button :icon="SwitchButton" @click="logout">退出</el-button>
        </div>
      </header>

      <main class="admin-content">
        <router-view v-slot="{ Component, route: childRoute }">
          <transition name="admin-route" mode="out-in">
            <component :is="Component" :key="childRoute.fullPath" />
          </transition>
        </router-view>
      </main>
    </section>
    <AdminCopilot />
    <ProactivePageGuide surface="admin" />
  </div>
</template>

<style scoped lang="scss">
.admin-layout {
  display: flex;
  min-height: 100dvh;
  width: 100%;
  background: #F5F7FB;
  color: var(--gray-900);
}

.admin-sidebar {
  width: 252px;
  flex: 0 0 252px;
  min-height: 100dvh;
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
  overflow-y: auto;
  padding: 12px 10px 18px;
}

.admin-nav-item {
  position: relative;
  width: 100%;
  min-height: 40px;
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
  display: flex;
  flex-direction: column;
}

.admin-header {
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

.admin-content {
  flex: 1;
  min-width: 0;
  padding: 24px 28px 36px;
  overflow: auto;
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
  }

  .admin-sidebar {
    width: 100%;
    min-height: auto;
    flex-basis: auto;
  }

  .admin-nav {
    display: flex;
    gap: 6px;
    overflow-x: auto;
    padding: 10px;
  }

  .admin-nav-item {
    width: auto;
    white-space: nowrap;
    margin-bottom: 0;
  }

  .admin-content {
    padding: 16px;
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
