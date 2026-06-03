<template>
  <div class="user-manage-page">
    <div class="page-header">
      <h2>用户管理</h2>
    </div>

    <el-card shadow="hover">
      <div class="filter-bar">
        <el-input
          v-model="keyword"
          placeholder="搜索用户名/邮箱"
          clearable
          style="width: 220px"
          @clear="loadData"
          @keyup.enter="loadData"
        />
        <el-select v-model="filterRole" placeholder="角色筛选" clearable style="width: 140px" @change="loadData">
          <el-option label="管理员" value="admin" />
          <el-option label="审查员" value="reviewer" />
          <el-option label="普通用户" value="user" />
        </el-select>
        <el-select v-model="filterStatus" placeholder="状态筛选" clearable style="width: 120px" @change="loadData">
          <el-option label="启用" :value="1" />
          <el-option label="禁用" :value="0" />
        </el-select>
        <el-button type="primary" @click="loadData">查询</el-button>
      </div>

      <el-table :data="users" v-loading="loading" style="width: 100%">
        <el-table-column prop="username" label="用户名" width="140" show-overflow-tooltip />
        <el-table-column prop="nickname" label="昵称" width="120" show-overflow-tooltip>
          <template #default="{ row }">{{ row.nickname || '-' }}</template>
        </el-table-column>
        <el-table-column prop="email" label="邮箱" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.email || '-' }}</template>
        </el-table-column>
        <el-table-column prop="role" label="角色" width="100">
          <template #default="{ row }">
            <el-tag :type="roleType(row.role)" size="small">{{ roleLabel(row.role) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.status ? 'success' : 'danger'" size="small">
              {{ row.status ? '启用' : '禁用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="last_login" label="最后登录" width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ row.last_login || '-' }}</template>
        </el-table-column>
        <el-table-column prop="create_time" label="注册时间" width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ row.create_time || '-' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="onSetRole(row)">设置角色</el-button>
            <el-button link :type="row.status ? 'warning' : 'success'" size="small" @click="onToggleStatus(row)">
              {{ row.status ? '禁用' : '启用' }}
            </el-button>
            <el-button link type="danger" size="small" @click="onResetPassword(row.id)">重置密码</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination-wrapper">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          @change="loadData"
        />
      </div>
    </el-card>

    <el-dialog v-model="roleDialogVisible" title="设置角色" width="400px">
      <el-form label-width="80px">
        <el-form-item label="用户">{{ selectedUser?.username }}</el-form-item>
        <el-form-item label="角色">
          <el-select v-model="selectedRole" placeholder="选择角色" style="width: 100%">
            <el-option label="普通用户" value="user" />
            <el-option label="审查员" value="reviewer" />
            <el-option label="管理员" value="admin" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="roleDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="onConfirmRole">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getUsers, setUserRole, toggleUserStatus, resetPassword } from '@/api/user'
import type { UserListItem } from '@/types/user'

const loading = ref(false)
const submitting = ref(false)
const users = ref<UserListItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const keyword = ref('')
const filterRole = ref('')
const filterStatus = ref<number | null>(null)

const roleDialogVisible = ref(false)
const selectedUser = ref<UserListItem | null>(null)
const selectedRole = ref('user')

const roleLabels: Record<string, string> = {
  admin: '管理员',
  reviewer: '审查员',
  user: '普通用户',
}

function roleLabel(role: string) {
  return roleLabels[role] ?? role
}

function roleType(role: string) {
  const map: Record<string, string> = { admin: 'danger', reviewer: 'warning', user: 'info' }
  return map[role] ?? 'info'
}

async function loadData() {
  loading.value = true
  try {
    const params: Record<string, unknown> = {
      page: page.value,
      page_size: pageSize.value,
    }
    if (keyword.value) params.keyword = keyword.value
    if (filterRole.value) params.role = filterRole.value
    if (filterStatus.value !== null) params.status = filterStatus.value

    const data = await getUsers(params)
    users.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

function onSetRole(row: UserListItem) {
  selectedUser.value = row
  selectedRole.value = row.role
  roleDialogVisible.value = true
}

async function onConfirmRole() {
  if (!selectedUser.value) return
  submitting.value = true
  try {
    await setUserRole(selectedUser.value.id, selectedRole.value)
    ElMessage.success('角色设置成功')
    roleDialogVisible.value = false
    loadData()
  } finally {
    submitting.value = false
  }
}

async function onToggleStatus(row: UserListItem) {
  const newStatus = row.status ? 0 : 1
  try {
    await ElMessageBox.confirm(
      `确定要${newStatus ? '启用' : '禁用'}用户「${row.username}」吗？`,
      '确认操作',
      { type: 'warning' },
    )
    await toggleUserStatus(row.id, newStatus)
    ElMessage.success('操作成功')
    loadData()
  } catch {
    /* canceled */
  }
}

async function onResetPassword(userId: number) {
  try {
    await ElMessageBox.confirm('确定要重置该用户的密码吗？', '确认重置密码', { type: 'warning' })
    const data = await resetPassword(userId)
    ElMessage.success(`密码已重置为: ${data.password}`)
  } catch {
    /* canceled */
  }
}

onMounted(() => {
  loadData()
})
</script>

<style scoped lang="scss">
.user-manage-page {
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
</style>
