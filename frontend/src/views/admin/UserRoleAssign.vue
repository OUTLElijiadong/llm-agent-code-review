<template>
  <div class="user-role-assign-page">
    <div class="page-header">
      <h2>用户角色分配</h2>
    </div>

    <el-card shadow="hover">
      <div class="filter-bar">
        <el-input
          v-model="keyword"
          placeholder="搜索用户名/邮箱"
          clearable
          style="width: 220px"
          @clear="loadUsers"
          @keyup.enter="loadUsers"
        />
        <el-select
          v-model="filterRoleCode"
          placeholder="按角色筛选"
          clearable
          style="width: 180px"
          @change="loadUsers"
        >
          <el-option
            v-for="r in allRoles"
            :key="r.id"
            :label="r.name"
            :value="r.code"
          />
        </el-select>
        <el-button type="primary" @click="loadUsers">查询</el-button>
      </div>

      <el-table :data="users" v-loading="loading" style="width: 100%">
        <el-table-column prop="username" label="用户名" width="140" show-overflow-tooltip />
        <el-table-column prop="nickname" label="昵称" width="120" show-overflow-tooltip>
          <template #default="{ row }">{{ row.nickname || '-' }}</template>
        </el-table-column>
        <el-table-column prop="email" label="邮箱" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">{{ row.email || '-' }}</template>
        </el-table-column>
        <el-table-column label="当前角色" min-width="220">
          <template #default="{ row }">
            <div v-if="row.roleNames.length" class="role-tags">
              <el-tag
                v-for="name in row.roleNames"
                :key="name"
                size="small"
                :type="roleTagType(name)"
              >
                {{ name }}
              </el-tag>
            </div>
            <span v-else class="text-muted">未分配</span>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.status ? 'success' : 'danger'" size="small">
              {{ row.status ? '启用' : '禁用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="onAssign(row)">分配角色</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="!filterRoleCode" class="pagination-wrapper">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          @change="loadUsers"
        />
      </div>
    </el-card>

    <!-- 分配角色对话框 -->
    <el-dialog v-model="assignDialogVisible" title="分配角色" width="480px" append-to-body>
      <el-form label-width="90px" v-loading="assignLoading">
        <el-form-item label="用户">{{ selectedUser?.username }}</el-form-item>
        <el-form-item label="邮箱">
          <span class="text-muted">{{ selectedUser?.email || '-' }}</span>
        </el-form-item>
        <el-form-item label="角色">
          <el-checkbox-group v-model="selectedRoleIds">
            <div class="role-checkbox-list">
              <el-checkbox
                v-for="r in allRoles"
                :key="r.id"
                :value="r.id"
              >
                <span class="role-cb-label">
                  <span class="role-cb-name">{{ r.name }}</span>
                  <span class="role-cb-code font-mono">{{ r.code }}</span>
                  <el-tag v-if="r.is_builtin" size="small" type="warning">内置</el-tag>
                </span>
              </el-checkbox>
            </div>
          </el-checkbox-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="assignDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="onConfirmAssign">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'

import { getUsers } from '@/api/user'
import {
  listRoles,
  fetchUsersByRole,
  fetchUserRoles,
  assignUserRoles,
} from '@/api/rbac'
import type { Role, UserRoleItem } from '@/types/rbac'
import { ElMessage } from 'element-plus/es/components/message/index'

/** 表格行数据(统一数据源形态) */
interface UserRow {
  /** 用户 ID */
  id: number
  /** 用户名 */
  username: string
  /** 昵称 */
  nickname?: string
  /** 邮箱 */
  email?: string
  /** 账号状态 */
  status: number
  /** 当前角色名称列表(用于展示) */
  roleNames: string[]
}

const loading = ref(false)
const submitting = ref(false)
const assignLoading = ref(false)
const users = ref<UserRow[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const keyword = ref('')
const filterRoleCode = ref('')

const allRoles = ref<Role[]>([])

const assignDialogVisible = ref(false)
const selectedUser = ref<UserRow | null>(null)
const selectedRoleIds = ref<number[]>([])

/** 角色名称到 tag 类型的映射(内置管理角色高亮) */
const ROLE_TAG_TYPE_MAP: Record<string, string> = {
  管理员: 'danger',
  审查员: 'warning',
}

/**
 * 根据角色名称返回 tag 类型
 * @param name - 角色名称
 * @returns Element Plus tag 类型
 */
function roleTagType(name: string): string {
  return ROLE_TAG_TYPE_MAP[name] || 'info'
}

/**
 * 将后端用户角色项转换为角色名称列表
 * @param roles - 用户角色列表
 * @returns 角色名称列表
 */
function toRoleNames(roles: UserRoleItem[]): string[] {
  return roles.map((r) => r.name)
}

/**
 * 加载全部角色(供筛选与分配对话框使用)
 * @returns void
 */
async function loadRoles(): Promise<void> {
  allRoles.value = await listRoles()
}

/**
 * 加载用户列表
 * - 选中角色筛选时,调用 fetchUsersByRole 并扁平化为表格行
 * - 未筛选时,调用 getUsers 分页接口
 * @returns void
 */
async function loadUsers(): Promise<void> {
  loading.value = true
  try {
    if (filterRoleCode.value) {
      // 按角色筛选:fetchUsersByRole 返回扁平用户列表,无分页
      const list = await fetchUsersByRole(filterRoleCode.value)
      const filtered = keyword.value
        ? list.filter(
            (u) =>
              u.username.toLowerCase().includes(keyword.value.toLowerCase()) ||
              (u.email || '').toLowerCase().includes(keyword.value.toLowerCase()),
          )
        : list
      users.value = filtered.map((u) => ({
        id: u.id,
        username: u.username,
        nickname: u.nickname,
        email: u.email,
        status: u.status,
        roleNames: [],
      }))
      // 异步补齐每个用户的角色名称展示
      loadRowRoleNames(users.value)
      total.value = filtered.length
    } else {
      const params: Record<string, unknown> = {
        page: page.value,
        page_size: pageSize.value,
      }
      if (keyword.value) params.keyword = keyword.value
      const data = await getUsers(params)
      users.value = data.items.map((u) => ({
        id: u.id,
        username: u.username,
        nickname: u.nickname,
        email: u.email,
        status: u.status,
        roleNames: [],
      }))
      // 异步补齐每个用户的 RBAC 角色名称展示
      loadRowRoleNames(users.value)
      total.value = data.total
    }
  } finally {
    loading.value = false
  }
}

/**
 * 异步批量补齐表格行的角色名称展示
 * @param rows - 表格行列表
 * @returns void
 */
async function loadRowRoleNames(rows: UserRow[]): Promise<void> {
  for (const row of rows) {
    try {
      const roles = await fetchUserRoles(row.id)
      // 通过 id 引用确保更新到响应式数据
      const target = users.value.find((u) => u.id === row.id)
      if (target) target.roleNames = toRoleNames(roles)
    } catch {
      /* 单个用户角色加载失败不影响整体 */
    }
  }
}

/**
 * 打开分配角色对话框,加载用户当前角色并预选
 * @param row - 表格行
 * @returns void
 */
async function onAssign(row: UserRow): Promise<void> {
  selectedUser.value = row
  selectedRoleIds.value = []
  assignDialogVisible.value = true
  assignLoading.value = true
  try {
    const roles = await fetchUserRoles(row.id)
    selectedRoleIds.value = roles.map((r) => r.id)
  } finally {
    assignLoading.value = false
  }
}

/**
 * 保存角色分配
 * @returns void
 */
async function onConfirmAssign(): Promise<void> {
  if (!selectedUser.value) return
  submitting.value = true
  try {
    await assignUserRoles(selectedUser.value.id, {
      user_id: selectedUser.value.id,
      role_ids: selectedRoleIds.value,
    })
    ElMessage.success('角色分配成功')
    assignDialogVisible.value = false
    loadUsers()
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  await loadRoles()
  loadUsers()
})
</script>

<style scoped lang="scss">
.user-role-assign-page {
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
  }
}

.filter-bar {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.pagination-wrapper {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

.role-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.text-muted {
  color: var(--el-text-color-secondary);
}

.role-checkbox-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.role-cb-label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.role-cb-name {
  font-weight: 500;
}

.role-cb-code {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
