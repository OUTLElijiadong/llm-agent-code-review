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

      <!-- 用户卡片列表:替代表格,头像+名称/徽章为主行,邮箱/登录/注册等元信息降级为次行 -->
      <div class="user-cards" v-loading="loading" role="list" data-testid="user-cards">
        <EmptyState v-if="!users.length" description="暂无用户" />
        <article
          v-for="row in users"
          :key="row.id"
          class="user-card"
          :class="{ 'is-disabled': !row.status }"
          :data-status="row.status ? 'active' : 'disabled'"
          role="listitem"
        >
          <span class="uc-band" :data-status="row.status ? 'active' : 'disabled'" aria-hidden="true"></span>
          <span
            class="uc-avatar"
            :data-status="row.status ? 'active' : 'disabled'"
            :aria-label="`用户 ${row.username} 头像`"
            :title="row.nickname ? `${row.nickname}(@${row.username})` : row.username"
          >{{ avatarInitial(row) }}</span>
          <div class="uc-main">
            <div class="uc-line1">
              <b class="uc-name" :title="row.nickname || row.username">{{ row.nickname || row.username }}</b>
              <span v-if="row.nickname" class="uc-username font-mono">@{{ row.username }}</span>
              <el-tag :type="roleType(row.role)" size="small">{{ roleLabel(row.role) }}</el-tag>
              <el-tag
                v-for="name in row.extraRoleNames ?? []"
                :key="name"
                type="info"
                size="small"
                effect="plain"
              >{{ name }}</el-tag>
              <el-tag :type="row.status ? 'success' : 'danger'" size="small">
                {{ row.status ? '启用' : '禁用' }}
              </el-tag>
            </div>
            <div class="uc-line2 font-mono">
              <span class="uc-email" :title="row.email || '-'">{{ row.email || '-' }}</span>
              <span>最后登录 {{ formatDateTime(row.last_login) }}</span>
              <span :title="row.last_login_ip || '-'">IP {{ row.last_login_ip || '-' }}</span>
              <span>注册 {{ formatDateTime(row.create_time) }}</span>
            </div>
          </div>
          <div class="uc-actions">
            <span v-if="row.username === 'admin'" class="protected-admin">唯一超级管理员</span>
            <template v-else>
              <el-button link type="primary" size="small" @click="onSetRole(row)">编辑角色</el-button>
              <el-button link :type="row.status ? 'warning' : 'success'" size="small" @click="onToggleStatus(row)">
                {{ row.status ? '禁用' : '启用' }}
              </el-button>
              <el-button link type="danger" size="small" @click="onResetPassword(row)">重置密码</el-button>
              <el-button link type="danger" size="small" @click="onDelete(row)">删除</el-button>
            </template>
          </div>
        </article>
      </div>

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

    <el-dialog v-model="roleDialogVisible" title="编辑角色" width="520px" append-to-body>
      <el-form label-width="90px" v-loading="roleLoading">
        <el-form-item label="用户">{{ selectedUser?.username }}</el-form-item>
        <el-form-item label="基础角色">
          <el-radio-group v-model="selectedRole">
            <el-radio value="user">普通用户(可管理自己的项目)</el-radio>
            <el-radio value="reviewer">审查员(可审查,项目操作受权限约束)</el-radio>
            <el-radio value="admin">管理员(程序内管理权限)</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="extraRoleOptions.length" label="附加角色">
          <el-checkbox-group v-model="selectedExtraRoleIds">
            <div class="extra-role-list">
              <el-checkbox v-for="r in extraRoleOptions" :key="r.id" :value="r.id">
                <span class="extra-role-label">
                  <span class="extra-role-name">{{ r.name }}</span>
                  <span class="extra-role-code font-mono">{{ r.code }}</span>
                  <el-tag v-if="r.is_builtin" size="small" type="warning">内置</el-tag>
                </span>
              </el-checkbox>
            </div>
          </el-checkbox-group>
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
          title="该用户创建的项目会保留(仍归其所有);审查员侧重审查,项目入口和操作按实时权限点与项目成员范围显示"
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
import { ref, computed, onMounted } from 'vue'
import { CopyDocument } from '@element-plus/icons-vue'

import EmptyState from '@/components/common/EmptyState.vue'
import { getUsers, toggleUserStatus, resetPassword, deleteUser } from '@/api/user'
import { listRoles, fetchUserRoles, assignUserRoles } from '@/api/rbac'
import type { Role } from '@/types/rbac'
import type { UserListItem } from '@/types/user'
import { formatDateTime } from '@/utils/format'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { ElMessage } from 'element-plus/es/components/message/index'

/** 表格行:在 UserListItem 基础上附挂 RBAC 附加角色名(展示用) */
interface UserRow extends UserListItem {
  extraRoleNames?: string[]
}

const loading = ref(false)
const submitting = ref(false)
const roleLoading = ref(false)
const users = ref<UserRow[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const keyword = ref('')
const filterRole = ref('')
const filterStatus = ref<number | null>(null)

const roleDialogVisible = ref(false)
const selectedUser = ref<UserRow | null>(null)
const selectedRole = ref('user')
const selectedExtraRoleIds = ref<number[]>([])
const allRoles = ref<Role[]>([])
const passwordDialogVisible = ref(false)
const resetPasswordUsername = ref('')
const temporaryPassword = ref('')

/** 基础角色(RBAC 内置编码,与 user.role 旧列一一对应) */
const BASE_ROLE_CODES = ['user', 'reviewer', 'admin'] as const
/** 附加角色选项:内置审计员 + 全部自定义角色;超级管理员任何入口都不可分配 */
const extraRoleOptions = computed(() => allRoles.value.filter(
  (r) => !(['user', 'reviewer', 'admin', 'super_admin'] as const).includes(r.code as never),
))

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

/** 头像首字母占位:优先昵称首字,回退用户名首字(不引入新组件)。 */
function avatarInitial(row: UserListItem): string {
  const source = (row.nickname || '').trim() || row.username
  return source ? source.charAt(0).toUpperCase() : '?'
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
    loadExtraRoleNames(users.value)
  } finally {
    loading.value = false
  }
}

/** 异步补齐每行的 RBAC 附加角色标签(基础角色已由 user.role 展示,跳过同名内置)。 */
async function loadExtraRoleNames(rows: UserRow[]): Promise<void> {
  for (const row of rows) {
    try {
      const roles = await fetchUserRoles(row.id)
      const target = users.value.find((u) => u.id === row.id)
      if (target) {
        target.extraRoleNames = roles
          .filter((r) => !(BASE_ROLE_CODES as readonly string[]).includes(r.code) && r.code !== 'super_admin')
          .map((r) => r.name)
      }
    } catch {
      /* 单个用户角色加载失败不影响整体 */
    }
  }
}

async function onSetRole(row: UserRow) {
  selectedUser.value = row
  selectedRole.value = BASE_ROLE_CODES.includes(row.role as never) ? row.role : 'user'
  selectedExtraRoleIds.value = []
  roleDialogVisible.value = true
  roleLoading.value = true
  try {
    // 以 RBAC 关联为权威源预选:基础角色取旧列,附加角色取非基础内置/自定义角色
    const roles = await fetchUserRoles(row.id)
    selectedExtraRoleIds.value = roles
      .filter((r) => !(BASE_ROLE_CODES as readonly string[]).includes(r.code) && r.code !== 'super_admin')
      .map((r) => r.id)
  } finally {
    roleLoading.value = false
  }
}

async function onConfirmRole() {
  if (!selectedUser.value) return
  submitting.value = true
  try {
    // 统一走 RBAC 覆盖式分配:基础角色+附加角色一次写入,
    // 后端由角色集合推导 user.role 旧列,两套显示口径不再分裂。
    const baseRole = allRoles.value.find((r) => r.code === selectedRole.value)
    if (!baseRole) throw new Error('基础角色不存在')
    await assignUserRoles(selectedUser.value.id, {
      user_id: selectedUser.value.id,
      role_ids: [baseRole.id, ...selectedExtraRoleIds.value],
    })
    selectedUser.value.role = selectedRole.value
    ElMessage.success('角色已保存')
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

onMounted(async () => {
  try {
    allRoles.value = await listRoles()
  } catch {
    /* 角色列表失败时仅隐藏附加角色区,基础功能可用 */
  }
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

/* ── 用户卡片列表(替代表格:头像+名称/徽章为主行,元信息降级为次行) ── */
.user-cards { display: grid; gap: 10px; min-height: 120px; }
.user-card {
  display: grid;
  grid-template-columns: auto auto minmax(0, 1fr) auto;
  gap: 14px; align-items: center;
  padding: 12px 16px 12px 12px; border-radius: 12px;
  background: #fff; border: 1px solid var(--gray-100, #eef0f4);
  transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease;
}
.user-card:hover {
  transform: translateY(-1.5px);
  box-shadow: 0 10px 24px rgba(23, 34, 62, .07);
  border-color: var(--brand-300, #a8c4fa);
}
.user-card.is-disabled .uc-name { color: var(--gray-500, #6e7689); }
.uc-band { width: 4px; height: 40px; border-radius: 999px; }
.uc-band[data-status='active'] { background: #40a35f; }
.uc-band[data-status='disabled'] { background: var(--sev-severe, #dc4961); }
.uc-avatar {
  width: 40px; height: 40px; border-radius: 50%;
  display: grid; place-items: center;
  font-size: 16px; font-weight: 600; color: #fff;
  background: linear-gradient(135deg, var(--brand-400, #6f9df7), var(--brand-600, #2f5ce0));
  user-select: none;
}
.uc-avatar[data-status='disabled'] {
  background: var(--gray-300, #cfd4dc);
  color: var(--gray-500, #6e7689);
}
.uc-main { display: grid; gap: 5px; min-width: 0; }
.uc-line1 { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.uc-name { font-size: 13.5px; font-weight: 600; max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.uc-username { font-size: 11.5px; color: var(--gray-500, #6e7689); max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.uc-line2 { display: flex; gap: 14px; flex-wrap: wrap; font-size: 11px; color: var(--gray-500, #6e7689); }
.uc-email { max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.uc-actions { display: flex; gap: 4px; align-items: center; justify-content: flex-end; flex-wrap: wrap; }

@media (prefers-reduced-motion: reduce) {
  .user-card { transition: none; }
  .user-card:hover { transform: none; box-shadow: none; }
}
@media (max-width: 760px) {
  .user-card { grid-template-columns: auto minmax(0, 1fr); }
  .uc-band { display: none; }
  .uc-actions { grid-column: 1 / -1; justify-content: flex-start; }
  .uc-line2 { gap: 8px; }
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

.extra-role-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.extra-role-label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.extra-role-name {
  font-weight: 500;
}

.extra-role-code {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.password-owner {
  margin: 18px 0 8px;
  color: var(--el-text-color-regular);
}

.temporary-password {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}
</style>
