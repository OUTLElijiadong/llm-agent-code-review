<template>
  <div class="permission-list-page">
    <div class="page-header">
      <h2>权限点列表</h2>
      <div class="header-tip">系统内置权限点(只读),共 {{ totalCount }} 个</div>
    </div>

    <el-card shadow="hover">
      <div class="filter-bar">
        <el-input
          v-model="keyword"
          placeholder="按编码或名称搜索权限点"
          clearable
          style="width: 280px"
          :prefix-icon="Search"
        />
      </div>

      <div v-loading="loading" class="perm-groups">
        <div v-for="group in filteredGroups" :key="group.module" class="perm-group">
          <div class="group-header">
            <el-icon class="group-icon"><Files /></el-icon>
            <span class="group-title">{{ group.label }}</span>
            <el-tag size="small" type="info">{{ group.items.length }} 个</el-tag>
          </div>
          <div class="perm-grid">
            <div v-for="perm in group.items" :key="perm.id" class="perm-card">
              <div class="perm-code font-mono">{{ perm.code }}</div>
              <div class="perm-name">{{ perm.name }}</div>
              <div class="perm-desc">{{ perm.description || '无描述' }}</div>
            </div>
          </div>
        </div>
        <el-empty v-if="filteredGroups.length === 0" description="未找到匹配的权限点" />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Search, Files } from '@element-plus/icons-vue'
import { listPermissions } from '@/api/rbac'
import type { Permission } from '@/types/rbac'

/** 权限点分组结构 */
interface PermGroup {
  /** 模块编码 */
  module: string
  /** 模块显示名称 */
  label: string
  /** 模块下权限点列表 */
  items: Permission[]
}

/** 模块中文标签映射(按 10 个模块排序展示) */
const MODULE_LABELS: Record<string, string> = {
  project: '项目管理',
  file: '文件管理',
  review: '代码审查',
  issue: '问题追踪',
  agent: 'Agent 中心',
  report: '审查报告',
  audit: '系统审计',
  user: '用户管理',
  role: '角色管理',
  menu: '菜单管理',
}

/** 模块展示顺序 */
const MODULE_ORDER = ['project', 'file', 'review', 'issue', 'agent', 'report', 'audit', 'user', 'role', 'menu']

const loading = ref(false)
const keyword = ref('')
const permissions = ref<Permission[]>([])

const totalCount = computed(() => permissions.value.length)

/**
 * 按模块分组并应用搜索过滤
 * @returns 分组后的权限点列表(仅含匹配项的分组)
 */
const filteredGroups = computed<PermGroup[]>(() => {
  const kw = keyword.value.trim().toLowerCase()
  const filtered = kw
    ? permissions.value.filter(
        (p) => p.code.toLowerCase().includes(kw) || p.name.toLowerCase().includes(kw),
      )
    : permissions.value

  const moduleMap = new Map<string, Permission[]>()
  for (const p of filtered) {
    const arr = moduleMap.get(p.module) || []
    arr.push(p)
    moduleMap.set(p.module, arr)
  }

  const groups: PermGroup[] = []
  for (const module of MODULE_ORDER) {
    const items = moduleMap.get(module)
    if (items && items.length > 0) {
      groups.push({
        module,
        label: MODULE_LABELS[module] || module,
        items,
      })
    }
  }
  // 处理未知模块(排序表外)
  for (const [module, items] of moduleMap.entries()) {
    if (!MODULE_ORDER.includes(module)) {
      groups.push({ module, label: MODULE_LABELS[module] || module, items })
    }
  }
  return groups
})

/**
 * 加载全部权限点
 * @returns void
 */
async function loadPermissions(): Promise<void> {
  loading.value = true
  try {
    permissions.value = await listPermissions()
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadPermissions()
})
</script>

<style scoped lang="scss">
.permission-list-page {
  .page-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 20px;

    h2 {
      margin: 0;
      font-size: 20px;
      font-weight: 600;
    }

    .header-tip {
      font-size: 13px;
      color: var(--el-text-color-secondary);
    }
  }
}

.filter-bar {
  margin-bottom: 20px;
}

.perm-groups {
  min-height: 120px;
}

.perm-group + .perm-group {
  margin-top: 24px;
  padding-top: 24px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.group-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;

  .group-icon {
    font-size: 16px;
    color: var(--el-color-primary);
  }

  .group-title {
    font-size: 15px;
    font-weight: 600;
  }
}

.perm-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
}

.perm-card {
  padding: 12px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-fill-color-blank);
  transition: border-color var(--el-transition-duration);

  &:hover {
    border-color: var(--el-color-primary-light-5);
  }
}

.perm-code {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-color-primary);
  margin-bottom: 4px;
}

.perm-name {
  font-size: 13px;
  color: var(--el-text-color-primary);
  margin-bottom: 4px;
}

.perm-desc {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.4;
}
</style>
