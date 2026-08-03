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
          <el-option label="超级管理员" value="super_admin" />
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
          <template #default="{ row }">{{ formatDateTime(row.last_login) }}</template>
        </el-table-column>
        <el-table-column prop="last_login_ip" label="最后登录 IP" width="140" show-overflow-tooltip>
          <template #default="{ row }">{{ row.last_login_ip || '-' }}</template>
        </el-table-column>
        <el-table-column prop="create_time" label="注册时间" width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ formatDateTime(row.create_time) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="300" fixed="right">
          <template #default="{ row }">
            <span v-if="row.username === 'admin'" class="protected-admin">唯一超级管理员</span>
            <template v-else>
            <el-button link type="primary" size="small" @click="onSetRole(row)">设置角色</el-button>
            <el-button link :type="row.status ? 'warning' : 'success'" size="small" @click="onToggleStatus(row)">
              {{ row.status ? '禁用' : '启用' }}
            </el-button>
            <el-button link type="danger" size="small" @click="onResetPassword(row)">重置密码</el-button>
            <el-button link type="danger" size="small" @click="onDelete(row)">删除</el-button>
            </template>
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

    <el-dialog v-model="roleDialogVisible" title="设置角色" width="460px" append-to-body>
      <el-form label-width="80px">
        <el-form-item label="用户">{{ selectedUser?.username }}</el-form-item>
        <el-form-item label="角色">
          <el-select v-model="selectedRole" placeholder="选择角色" style="width: 100%">
            <el-option label="普通用户(可管理自己的项目)" value="user" />
            <el-option label="审查员(可审查,不管理项目)" value="reviewer" />
            <el-option label="管理员(程序内管理权限)" value="admin" />
          </el-select>
        </el-form-item>
        <!-- 项目影响说明:角色变更不影响已建项目归属,仅改变后续可见范围 -->
        <el-alert
          v-if="selectedRole === 'admin'"
          type="success"
          :closable="false"
          show-icon
          title="该用户创建的项目与代码会保留,且变为管理员后可查看/管理全部用户的项目"
        />
        <el-alert
          v-else-if="selectedRole === 'reviewer'"
          type="warning"
          :closable="false"
          show-icon
          title="该用户创建的项目会保留(仍归其所有),但审查员侧重审查,不再显示「项目管理」入口;如需管理项目请改回普通用户"
        />
        <el-alert
          v-else
          type="info"
          :closable="false"
          show-icon
          title="该用户创建的项目会保留,仅可管理自己的项目"
        />
      </el-form>
      <template #footer>
        <el-button @click="roleDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="onConfirmRole">确定</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="passwordDialogVisible"
      title="密码已重置"
      width="min(520px, 92vw)"
      destroy-on-close
      @closed="clearTemporaryPassword"
    >
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="旧设备会话已强制下线。随机密码仅显示这一次，请立即通过安全渠道交给用户。"
      />
      <p class="password-owner">账号：{{ resetPasswordUsername }}</p>
      <el-input :model-value="temporaryPassword" readonly class="temporary-password">
        <template #append>
          <el-tooltip content="复制随机密码" placement="top">
            <el-button :icon="CopyDocument" aria-label="复制随机密码" @click="copyTemporaryPassword" />
          </el-tooltip>
        </template>
      </el-input>
      <template #footer>
        <el-button type="primary" @click="passwordDialogVisible = false">我已保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { CopyDocument } from '@element-plus/icons-vue'

import { getUsers, setUserRole, toggleUserStatus, resetPassword, deleteUser } from '@/api/user'
import type { UserListItem } from '@/types/user'
import { formatDateTime } from '@/utils/format'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { ElMessage } from 'element-plus/es/components/message/index'

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
const passwordDialogVisible = ref(false)
const resetPasswordUsername = ref('')
const temporaryPassword = ref('')

const roleLabels: Record<string, string> = {
  super_admin: '超级管理员',
  admin: '管理员',
  reviewer: '审查员',
  user: '普通用户',
}

function roleLabel(role: string) {
  return roleLabels[role] ?? role
}

function roleType(role: string) {
  const map: Record<string, string> = { super_admin: 'danger', admin: 'warning', reviewer: 'warning', user: 'info' }
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

async function onResetPassword(row: UserListItem) {
  try {
    await ElMessageBox.confirm(`确定要重置用户「${row.username}」的密码并强制下线其旧会话吗？`, '确认重置密码', {
      type: 'warning',
    })
    const data = await resetPassword(row.id)
    resetPasswordUsername.value = row.username
    temporaryPassword.value = data.temporary_password
    passwordDialogVisible.value = true
  } catch {
    /* canceled */
  }
}

async function copyTemporaryPassword() {
  try {
    await navigator.clipboard.writeText(temporaryPassword.value)
    ElMessage.success('随机密码已复制')
  } catch {
    ElMessage.warning('浏览器未允许自动复制，请手动选择密码')
  }
}

function clearTemporaryPassword() {
  temporaryPassword.value = ''
  resetPasswordUsername.value = ''
}

async function onDelete(row: UserListItem) {
  try {
    await ElMessageBox.confirm(
      `确定要删除用户「${row.username}」吗?\n删除为软删除:该账号将无法登录,但其项目与历史数据会保留。`,
      '确认删除用户',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
    await deleteUser(row.id)
    ElMessage.success(`用户「${row.username}」已删除`)
    loadData()
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

.protected-admin {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.password-owner {
  margin: 18px 0 8px;
  color: var(--el-text-color-regular);
}

.temporary-password {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}
</style>
