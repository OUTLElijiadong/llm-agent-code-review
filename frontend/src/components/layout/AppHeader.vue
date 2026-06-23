<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowDown, SwitchButton, UserFilled, Search, MagicStick, Menu } from '@element-plus/icons-vue'
import { ElMessageBox } from 'element-plus'
import { useUserStore } from '@/stores/user'
import { canRoleOpenPath, normalizeRole, type UserRole } from '@/utils/roleHome'

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const emit = defineEmits<{
  (e: 'toggle-sidebar'): void
}>()

interface SearchItem {
  title: string
  description: string
  path?: string
  action?: 'agent'
  admin?: boolean
  roles?: UserRole[]
}

const searchVisible = ref(false)
const searchKeyword = ref('')
const openAgentChat = inject<() => void>('openAgentChat', () => {})

const crumbs = computed(() => {
  const titles: string[] = []
  for (const match of route.matched) {
    const t = match.meta?.title as string | undefined
    if (t && !titles.includes(t)) titles.push(t)
  }
  if (titles.length === 0 && route.meta?.title) {
    titles.push(route.meta.title as string)
  }
  return titles
})

const roleLabel = computed(() => {
  const role = userStore.profile?.role
  if (role === 'admin') return '管理员'
  if (role === 'reviewer') return '审查员'
  return '普通用户'
})

const isAdmin = computed(() => userStore.profile?.role === 'admin')
const currentRole = computed(() => normalizeRole(userStore.profile?.role))

const searchItems = computed<SearchItem[]>(() => {
  const items: SearchItem[] = [
    { title: '工作台', description: '查看审查任务、风险分布和最近活动', path: '/dashboard', roles: ['admin', 'user', 'reviewer'] },
    { title: '项目管理', description: '管理项目、上传代码文件和编辑项目信息', path: '/projects', roles: ['user'] },
    { title: '发起审查', description: '选择项目文件并启动 Agent 代码审查', path: '/reviews/start', roles: ['user', 'reviewer'] },
    { title: '审查记录', description: '查看历史审查任务和审查状态', path: '/reviews', roles: ['user', 'reviewer'] },
    { title: '审查规则', description: '配置代码规范、性能、安全等审查维度', path: '/rules', roles: ['user', 'reviewer'] },
    { title: '审查报告', description: '查看和导出审查报告', path: '/reports', roles: ['admin', 'user', 'reviewer'] },
    { title: '修改密码', description: '更新当前账号登录密码', path: '/profile/password', roles: ['admin', 'user', 'reviewer'] },
    { title: 'Agent 助手', description: '打开智能助手咨询代码审查问题', action: 'agent' },
    { title: '用户管理', description: '管理平台用户和角色权限', path: '/admin/users', admin: true },
    { title: 'Agent 调用日志', description: '查看大模型调用状态和异常日志', path: '/admin/ai-logs', admin: true },
    { title: '系统操作审计', description: '查看管理员操作和平台审计记录', path: '/admin/audit', admin: true },
    { title: 'Agent 自进化', description: '审批和回滚 Agent 经验提案', path: '/admin/evolution', admin: true },
  ]
  return items.filter((item) => {
    if (item.admin && !isAdmin.value) return false
    if (item.roles && !item.roles.includes(currentRole.value)) return false
    if (item.path && !canRoleOpenPath(currentRole.value, item.path)) return false
    return true
  })
})

const filteredSearchItems = computed(() => {
  const keyword = searchKeyword.value.trim().toLowerCase()
  if (!keyword) return searchItems.value
  return searchItems.value.filter((item) => {
    const haystack = `${item.title} ${item.description} ${item.path ?? ''}`.toLowerCase()
    return haystack.includes(keyword)
  })
})

/**
 * 切换移动端侧边栏显示状态
 * @returns void
 */
function toggleSidebar(): void {
  emit('toggle-sidebar')
}

/**
 * 打开全局搜索面板
 * @returns void
 */
function openSearch(): void {
  searchKeyword.value = ''
  searchVisible.value = true
}

/**
 * 关闭全局搜索面板
 * @returns void
 */
function closeSearch(): void {
  searchVisible.value = false
}

/**
 * 执行全局搜索结果对应的导航或动作
 * @param item - 用户选择的搜索结果
 * @returns void
 */
function runSearchItem(item: SearchItem): void {
  closeSearch()
  if (item.action === 'agent') {
    openAgent()
    return
  }
  if (item.path && item.path !== route.path) {
    router.push(item.path)
  }
}

/**
 * 监听 Cmd/Ctrl + K 快捷键并打开全局搜索
 * @param event - 键盘事件
 * @returns void
 */
function handleGlobalShortcut(event: KeyboardEvent): void {
  const isSearchShortcut = (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k'
  if (!isSearchShortcut) return
  event.preventDefault()
  openSearch()
}

/**
 * 确认并执行退出登录
 * @returns Promise<void>
 */
async function handleLogout(): Promise<void> {
  try {
    await ElMessageBox.confirm('确定要退出登录吗？', '提示', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning',
    })
    userStore.logout()
    router.push('/login')
  } catch {
    /* 用户取消 */
  }
}

/**
 * 跳转到修改密码页面
 * @returns void
 */
function goChangePassword(): void {
  router.push('/profile/password')
}

/**
 * 打开 Agent 助手抽屉
 * @returns void
 */
function openAgent(): void {
  openAgentChat()
}

onMounted(() => {
  window.addEventListener('keydown', handleGlobalShortcut)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleGlobalShortcut)
})
</script>

<template>
  <header class="app-header">
    <div class="header-left">
      <button class="menu-trigger" type="button" aria-label="打开导航菜单" @click="toggleSidebar">
        <el-icon><Menu /></el-icon>
      </button>
      <span class="crumb-home font-mono">PRISM</span>
      <span class="crumb-sep">/</span>
      <template v-for="(c, i) in crumbs" :key="c">
        <span class="crumb" :class="{ 'is-current': i === crumbs.length - 1 }">{{ c }}</span>
        <span v-if="i < crumbs.length - 1" class="crumb-sep">/</span>
      </template>
    </div>

    <div class="header-right">
      <button class="search-trigger" type="button" aria-label="打开全局搜索" @click="openSearch">
        <el-icon><Search /></el-icon>
        <span class="search-label">全局搜索</span>
        <span class="search-kbd font-mono">⌘K</span>
      </button>

      <button class="agent-trigger" type="button" @click="openAgent">
        <el-icon class="agent-icon"><MagicStick /></el-icon>
        <span>Agent</span>
      </button>

      <el-dropdown trigger="click">
        <div class="user-info">
          <span class="user-avatar">
            <el-icon><UserFilled /></el-icon>
          </span>
          <span class="user-meta">
            <span class="user-name">{{ userStore.displayName || '未登录' }}</span>
            <span class="user-role font-mono">{{ roleLabel }}</span>
          </span>
          <el-icon class="arrow-icon"><ArrowDown /></el-icon>
        </div>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item @click="goChangePassword">修改密码</el-dropdown-item>
            <el-dropdown-item divided @click="handleLogout">
              <span class="logout-item">
                <el-icon><SwitchButton /></el-icon>
                退出登录
              </span>
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>
  </header>

  <el-dialog
    v-model="searchVisible"
    title="全局搜索"
    width="560px"
    class="command-dialog"
    append-to-body
  >
    <div class="command-panel">
      <el-input
        v-model="searchKeyword"
        placeholder="搜索页面、功能或 Agent 助手"
        clearable
        autofocus
        :prefix-icon="Search"
      />
      <div class="command-list">
        <button
          v-for="item in filteredSearchItems"
          :key="item.title"
          type="button"
          class="command-item"
          @click="runSearchItem(item)"
        >
          <span class="command-title">{{ item.title }}</span>
          <span class="command-desc">{{ item.description }}</span>
        </button>
        <div v-if="filteredSearchItems.length === 0" class="command-empty">
          没有匹配的功能
        </div>
      </div>
    </div>
  </el-dialog>
</template>

<style scoped lang="scss">
.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  height: var(--header-height);
  padding: 0 24px;
  background: var(--surface-glass);
  border-bottom: var(--hairline);
  box-shadow: 0 1px 0 rgba(255, 255, 255, 0.72) inset;
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  flex-shrink: 0;
  min-width: 0;
}

/* 面包屑 ------------------------------- */
.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--color-text-regular);
  flex: 1;
  min-width: 0;
  overflow: hidden;
}

.menu-trigger {
  display: none;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border: var(--hairline);
  border-radius: 8px;
  background: var(--surface-1);
  color: var(--color-text-regular);
  cursor: pointer;
  transition: border-color var(--transition-fast), color var(--transition-fast), background var(--transition-fast);

  &:hover {
    border-color: var(--brand-200);
    color: var(--brand-600);
    background: var(--brand-50);
  }
}

.crumb-home {
  font-size: 11px;
  letter-spacing: 0.16em;
  color: var(--brand-600);
  font-weight: 600;
}

.crumb-sep {
  color: var(--color-border-base);
}

.crumb {
  color: var(--color-text-regular);
  white-space: nowrap;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;

  &.is-current {
    color: var(--color-text-primary);
    font-weight: 500;
  }
}

/* 右侧操作 ----------------------------- */
.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
  flex-shrink: 0;
}

.search-trigger {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 36px;
  min-width: 238px;
  padding: 0 10px 0 12px;
  border: var(--hairline);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.72);
  color: var(--color-text-secondary);
  font-size: 12.5px;
  cursor: pointer;
  transition: all var(--transition-fast);

  &:hover {
    border-color: var(--brand-200);
    background: #fff;
    color: var(--color-text-primary);
    box-shadow: var(--shadow-1);
  }

  .el-icon {
    font-size: 14px;
  }
}

.search-label {
  flex: 1;
  min-width: 64px;
  text-align: left;
}

.search-kbd {
  padding: 1px 6px;
  border-radius: 4px;
  background: #fff;
  border: 1px solid var(--color-border-light);
  font-size: 10.5px;
  color: var(--color-text-secondary);
}

.agent-trigger {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 36px;
  padding: 0 14px;
  border: 1px solid var(--brand-200);
  border-radius: 8px;
  background: linear-gradient(135deg, #fff, var(--brand-50));
  color: var(--brand-600);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all var(--transition-fast);

  &:hover {
    border-color: var(--brand-400);
    background: #fff;
    box-shadow: 0 6px 16px -10px rgba(91, 88, 232, 0.52);
  }
}

.agent-icon {
  font-size: 14px;
  color: var(--brand-500);
}

/* 用户区 ------------------------------ */
.user-info {
  display: flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
  padding: 4px 10px 4px 4px;
  border-radius: 8px;
  transition: background var(--transition-fast), box-shadow var(--transition-fast);

  &:hover {
    background: rgba(255, 255, 255, 0.78);
    box-shadow: var(--shadow-1);
  }
}

.user-avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: linear-gradient(135deg, var(--brand-500), var(--accent-400));
  color: #fff;
  font-size: 14px;
}

.user-meta {
  display: flex;
  flex-direction: column;
  gap: 1px;
  line-height: 1;
}

.user-name {
  font-size: 13px;
  color: var(--color-text-primary);
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.user-role {
  font-size: 10px;
  letter-spacing: 0.06em;
  color: var(--color-text-secondary);
}

.arrow-icon {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.logout-item {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--color-danger);
}

.command-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.command-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: min(420px, 58vh);
  overflow-y: auto;
}

.command-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 100%;
  padding: 12px 14px;
  border: 1px solid var(--gray-100);
  border-radius: 8px;
  background: #fff;
  text-align: left;
  cursor: pointer;
  transition: border-color var(--transition-fast), background var(--transition-fast), transform var(--transition-fast);

  &:hover {
    border-color: var(--brand-200);
    background: var(--brand-50);
    transform: translateY(-1px);
  }
}

.command-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--gray-900);
}

.command-desc {
  font-size: 12px;
  color: var(--gray-500);
  line-height: 1.4;
}

.command-empty {
  padding: 28px 12px;
  text-align: center;
  color: var(--gray-500);
  font-size: 13px;
}

:deep(.command-dialog) {
  max-width: calc(100vw - 24px);
}

@media (max-width: 1100px) {
  .app-header {
    gap: 16px;
    padding: 0 18px;
  }

  .search-trigger {
    min-width: 180px;
  }

  .user-name {
    max-width: 96px;
  }
}

@media (max-width: 920px) {
  .search-trigger {
    width: 44px;
    min-width: 44px;
    padding: 0;
    justify-content: center;
  }

  .search-label,
  .search-kbd {
    display: none;
  }
}

@media (max-width: 768px) {
  .app-header {
    gap: 12px;
    padding: 0 12px;
  }

  .menu-trigger {
    display: inline-flex;
  }

  .crumb-home,
  .crumb-sep,
  .crumb:not(.is-current) {
    display: none;
  }

  .header-right {
    gap: 8px;
  }

  .search-trigger {
    width: 34px;
    min-width: 34px;
    padding: 0;
    justify-content: center;
  }

  .search-label,
  .search-kbd,
  .agent-trigger span,
  .user-meta,
  .arrow-icon {
    display: none;
  }

  .agent-trigger {
    width: 34px;
    padding: 0;
    justify-content: center;
  }
}

@media (max-width: 420px) {
  .app-header {
    gap: 8px;
    padding: 0 10px;
  }

  .header-right {
    gap: 6px;
  }

  .user-info {
    padding: 0;
  }
}
</style>
