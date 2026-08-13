<template>
  <div class="role-manage-page">
    <div class="page-header">
      <h2>角色管理</h2>
      <el-button type="primary" @click="onCreate">新建角色</el-button>
    </div>

    <el-card shadow="hover">
      <el-table :data="roles" v-loading="loading" style="width: 100%">
        <el-table-column prop="code" label="角色编码" width="160" show-overflow-tooltip />
        <el-table-column prop="name" label="角色名称" width="160" show-overflow-tooltip />
        <el-table-column prop="description" label="描述" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">{{ row.description || '-' }}</template>
        </el-table-column>
        <el-table-column prop="is_builtin" label="内置" width="90">
          <template #default="{ row }">
            <el-tag :type="row.is_builtin ? 'warning' : 'info'" size="small">
              {{ row.is_builtin ? '内置' : '自定义' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="320" fixed="right">
          <template #default="{ row }">
            <span v-if="row.code === 'super_admin'" class="protected-role">固定最高权限</span>
            <template v-else>
            <el-button link type="primary" size="small" @click="onEdit(row)">编辑</el-button>
            <el-button link type="success" size="small" @click="onAssignPermissions(row)">分配权限</el-button>
            <el-button link type="warning" size="small" @click="onSetDataScope(row)">数据范围</el-button>
            <el-button
              link
              type="danger"
              size="small"
              :disabled="row.is_builtin"
              @click="onDelete(row)"
            >
              删除
            </el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新建/编辑角色对话框 -->
    <el-dialog
      v-model="formDialogVisible"
      :title="editingRole ? '编辑角色' : '新建角色'"
      width="460px"
    >
      <el-form ref="formRef" :model="formData" :rules="formRules" label-width="90px">
        <el-form-item label="角色编码" prop="code">
          <el-input
            v-model="formData.code"
            placeholder="如 reviewer / auditor"
            :disabled="!!editingRole"
          />
        </el-form-item>
        <el-form-item label="角色名称" prop="name">
          <el-input v-model="formData.name" placeholder="角色显示名称" />
        </el-form-item>
        <el-form-item label="描述" prop="description">
          <el-input v-model="formData.description" type="textarea" :rows="3" placeholder="可选" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="formDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="onConfirmForm">确定</el-button>
      </template>
    </el-dialog>

    <!-- 分配权限抽屉 -->
    <el-drawer
      v-model="permDrawerVisible"
      :title="`分配权限 - ${currentRole?.name || ''}`"
      size="420px"
      direction="rtl"
    >
      <div v-loading="permLoading" class="perm-drawer-body">
        <el-tree
          ref="permTreeRef"
          :data="permTreeData"
          :props="{ label: 'label', children: 'children' }"
          node-key="id"
          show-checkbox
          default-expand-all
          :check-strictly="false"
        />
      </div>
      <template #footer>
        <el-button @click="permDrawerVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="onConfirmPermissions">保存</el-button>
      </template>
    </el-drawer>

    <!-- 数据范围设置对话框 -->
    <el-dialog v-model="scopeDialogVisible" title="设置数据范围" width="480px">
      <el-form label-width="100px" v-loading="scopeLoading">
        <el-form-item label="角色">{{ currentRole?.name }}</el-form-item>
        <el-form-item label="范围类型">
          <el-radio-group v-model="scopeForm.scope_type">
            <el-radio value="all">全部数据</el-radio>
            <el-radio value="custom">指定项目</el-radio>
            <el-radio value="project_own">仅本人</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="scopeForm.scope_type === 'custom'" label="项目列表">
          <el-select
            v-model="scopeForm.project_ids"
            multiple
            filterable
            placeholder="选择可访问的项目"
            style="width: 100%"
          >
            <el-option
              v-for="p in projectOptions"
              :key="p.id"
              :label="p.project_name"
              :value="p.id"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="scopeDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="onConfirmScope">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'

import type { FormInstance, FormRules } from 'element-plus'
import {
  listRoles,
  createRole,
  updateRole,
  deleteRole,
  listPermissions,
  fetchRolePermissions,
  assignRolePermissions,
  fetchRoleDataScope,
  updateRoleDataScope,
} from '@/api/rbac'
import { getProjects } from '@/api/project'
import type { Role, Permission, DataScopeType, DataScopeUpdateIn } from '@/types/rbac'
import type { ProjectOut } from '@/types/project'
import { ElMessage } from 'element-plus/es/components/message/index'
import { confirmDanger } from '@/composables/useDangerConfirm'

/** 权限树节点 */
interface PermTreeNode {
  /** 节点 ID:模块节点为字符串,权限点节点为数字 ID */
  id: string | number
  /** 节点显示文本 */
  label: string
  /** 子节点 */
  children?: PermTreeNode[]
}

/** el-tree 实例需要用到的方法子集(避免引入复杂组件实例类型) */
interface ElTreeInstance {
  setCheckedKeys: (keys: (string | number)[], leafOnly?: boolean) => void
  getCheckedKeys: (leafOnly: boolean) => unknown
  getHalfCheckedKeys: () => unknown
}

/** 模块中文标签映射 */
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

const loading = ref(false)
const submitting = ref(false)
const roles = ref<Role[]>([])

const formDialogVisible = ref(false)
const formRef = ref<FormInstance>()
const editingRole = ref<Role | null>(null)
const formData = reactive<{ code: string; name: string; description: string }>({
  code: '',
  name: '',
  description: '',
})
const formRules: FormRules = {
  code: [{ required: true, message: '请输入角色编码', trigger: 'blur' }],
  name: [{ required: true, message: '请输入角色名称', trigger: 'blur' }],
}

const permDrawerVisible = ref(false)
const permLoading = ref(false)
const permTreeRef = ref<ElTreeInstance>()
const currentRole = ref<Role | null>(null)
const permTreeData = ref<PermTreeNode[]>([])
const allPermissions = ref<Permission[]>([])

const scopeDialogVisible = ref(false)
const scopeLoading = ref(false)
const scopeForm = reactive<{ scope_type: DataScopeType; project_ids: number[] }>({
  scope_type: 'project_own',
  project_ids: [],
})
const projectOptions = ref<ProjectOut[]>([])

/**
 * 加载角色列表
 * @returns void
 */
async function loadRoles(): Promise<void> {
  loading.value = true
  try {
    roles.value = await listRoles()
  } finally {
    loading.value = false
  }
}

/**
 * 加载全部权限点并构建按模块分组的权限树
 * @returns void
 */
async function loadPermissions(): Promise<void> {
  if (allPermissions.value.length > 0) {
    permTreeData.value = buildPermTree(allPermissions.value)
    return
  }
  const list = await listPermissions()
  allPermissions.value = list
  permTreeData.value = buildPermTree(list)
}

/**
 * 将扁平权限点列表构建为按模块分组的树形结构
 * @param list - 权限点列表
 * @returns 权限树
 */
function buildPermTree(list: Permission[]): PermTreeNode[] {
  const moduleMap = new Map<string, Permission[]>()
  for (const p of list) {
    const arr = moduleMap.get(p.module) || []
    arr.push(p)
    moduleMap.set(p.module, arr)
  }
  const tree: PermTreeNode[] = []
  for (const [module, perms] of moduleMap.entries()) {
    tree.push({
      id: `module-${module}`,
      label: MODULE_LABELS[module] || module,
      children: perms.map((p) => ({ id: p.id, label: `${p.code} · ${p.name}` })),
    })
  }
  return tree
}

/**
 * 重置表单数据
 * @returns void
 */
function resetForm(): void {
  formData.code = ''
  formData.name = ''
  formData.description = ''
  editingRole.value = null
}

/**
 * 打开新建角色对话框
 * @returns void
 */
function onCreate(): void {
  resetForm()
  formDialogVisible.value = true
}

/**
 * 打开编辑角色对话框
 * @param row - 角色行数据
 * @returns void
 */
function onEdit(row: Role): void {
  editingRole.value = row
  formData.code = row.code
  formData.name = row.name
  formData.description = row.description || ''
  formDialogVisible.value = true
}

/**
 * 提交新建/编辑角色表单
 * @returns void
 */
async function onConfirmForm(): Promise<void> {
  if (!formRef.value) return
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  submitting.value = true
  try {
    if (editingRole.value) {
      await updateRole(editingRole.value.id, {
        name: formData.name,
        description: formData.description,
      })
      ElMessage.success('角色更新成功')
    } else {
      await createRole({
        code: formData.code,
        name: formData.name,
        description: formData.description,
      })
      ElMessage.success('角色创建成功')
    }
    formDialogVisible.value = false
    loadRoles()
  } finally {
    submitting.value = false
  }
}

/**
 * 删除角色(内置角色禁用)
 * @param row - 角色行数据
 * @returns void
 */
async function onDelete(row: Role): Promise<void> {
  if (!await confirmDanger({ target: `删除角色「${row.name}」`, consequence: '该操作不可恢复' })) return
  try {
    await deleteRole(row.id)
    ElMessage.success('角色已删除')
    loadRoles()
  } catch {
    /* http 拦截器已提示 */
  }
}

/**
 * 打开分配权限抽屉,加载当前角色已有权限并勾选
 * @param row - 角色行数据
 * @returns void
 */
async function onAssignPermissions(row: Role): Promise<void> {
  currentRole.value = row
  permDrawerVisible.value = true
  permLoading.value = true
  try {
    await loadPermissions()
    const owned = await fetchRolePermissions(row.id)
    const ownedIds = owned.map((p) => p.id)
    // 等待树渲染后设置勾选
    await nextFrame()
    permTreeRef.value?.setCheckedKeys(ownedIds, false)
  } finally {
    permLoading.value = false
  }
}

/**
 * 等待下一帧,确保 el-tree 已渲染
 * @returns Promise
 */
function nextFrame(): Promise<void> {
  return new Promise((resolve) => requestAnimationFrame(() => resolve()))
}

/**
 * 保存权限分配
 * @returns void
 */
async function onConfirmPermissions(): Promise<void> {
  if (!currentRole.value || !permTreeRef.value) return
  submitting.value = true
  try {
    const checked = permTreeRef.value.getCheckedKeys(false) as (string | number)[]
    const halfChecked = permTreeRef.value.getHalfCheckedKeys() as (string | number)[]
    // 仅保留数字 ID(权限点),过滤掉模块节点
    const permissionIds = [...checked, ...halfChecked].filter(
      (k): k is number => typeof k === 'number',
    )
    await assignRolePermissions(currentRole.value.id, { permission_ids: permissionIds })
    ElMessage.success('权限分配成功')
    permDrawerVisible.value = false
  } finally {
    submitting.value = false
  }
}

/**
 * 打开数据范围设置对话框,并同时加载当前值和项目选项。
 * @param row - 角色行数据
 * @returns void
 */
async function onSetDataScope(row: Role): Promise<void> {
  currentRole.value = row
  scopeDialogVisible.value = true
  scopeLoading.value = true
  try {
    const [scope, projectPage] = await Promise.all([
      fetchRoleDataScope(row.id),
      projectOptions.value.length === 0
        ? getProjects({ page: 1, page_size: 200 })
        : Promise.resolve(null),
    ])
    scopeForm.scope_type = scope?.scope_type ?? 'project_own'
    scopeForm.project_ids = [...(scope?.project_ids ?? [])]
    if (projectPage) projectOptions.value = projectPage.items
  } catch {
    scopeDialogVisible.value = false
    ElMessage.error('数据范围加载失败')
  } finally {
    scopeLoading.value = false
  }
}

/**
 * 保存数据范围设置
 * @returns void
 */
async function onConfirmScope(): Promise<void> {
  if (!currentRole.value) return
  if (scopeForm.scope_type === 'custom' && scopeForm.project_ids.length === 0) {
    ElMessage.warning('请选择至少一个项目')
    return
  }
  submitting.value = true
  try {
    const payload: DataScopeUpdateIn = {
      scope_type: scopeForm.scope_type,
      project_ids: scopeForm.scope_type === 'custom' ? scopeForm.project_ids : undefined,
    }
    await updateRoleDataScope(currentRole.value.id, payload)
    ElMessage.success('数据范围已更新')
    scopeDialogVisible.value = false
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  loadRoles()
})
</script>

<style scoped lang="scss">
.role-manage-page {
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

.perm-drawer-body {
  padding: 0 16px 16px;
  min-height: 200px;
}

.protected-role {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>
