import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const rbacApi = vi.hoisted(() => ({
  listRoles: vi.fn(),
  updateRole: vi.fn(),
  listPermissions: vi.fn(),
  fetchRolePermissions: vi.fn(),
  assignRolePermissions: vi.fn(),
  fetchRoleDataScope: vi.fn(),
  updateRoleDataScope: vi.fn(),
}))
const projectApi = vi.hoisted(() => ({ getProjects: vi.fn() }))
const messages = vi.hoisted(() => ({ success: vi.fn(), warning: vi.fn(), error: vi.fn() }))

vi.mock('@/api/rbac', () => rbacApi)
vi.mock('@/api/project', () => projectApi)
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: messages }))

import RoleManage from './RoleManage.vue'

const role = {
  id: 10,
  code: 'reviewer',
  name: '评审员',
  description: '代码评审',
  status: 'active',
  sort: 100,
  is_builtin: false,
}

function mountRoleManage(): VueWrapper {
  return shallowMount(RoleManage, {
    global: {
      stubs: {
        'el-button': true,
        'el-card': true,
        'el-alert': { props: ['title'], template: '<div role="alert">{{ title }}<slot /></div>' },
        'el-dialog': true,
        'el-drawer': true,
        'el-form': true,
        'el-form-item': true,
        'el-input': true,
        'el-option': true,
        'el-radio': true,
        'el-radio-group': true,
        'el-select': true,
        'el-table': true,
        'el-table-column': { template: '<div />' },
        'el-tag': true,
        'el-tree': true,
      },
      directives: { loading: () => undefined },
    },
  })
}

function setupState(wrapper: VueWrapper): Record<string, any> {
  return (wrapper.vm as unknown as { $: { setupState: Record<string, any> } }).$.setupState
}

beforeEach(() => {
  vi.clearAllMocks()
  rbacApi.listRoles.mockResolvedValue([role])
  rbacApi.fetchRoleDataScope.mockResolvedValue({
    id: 1,
    role_id: role.id,
    scope_type: 'custom',
    project_ids: [22],
  })
  projectApi.getProjects.mockResolvedValue({
    items: [{ id: 22, project_name: '现有项目' }],
    total: 1,
    page: 1,
    page_size: 200,
  })
})

describe('RoleManage data scope', () => {
  it('统一 reviewer 固定角色的展示名称，同时保留其他角色的服务端名称', async () => {
    const wrapper = mountRoleManage()
    await flushPromises()

    const state = setupState(wrapper)
    expect(state.roleNameText(role)).toBe('审查员')
    expect(state.roleNameText({ ...role, code: 'user', name: '普通用户' })).toBe('普通用户')
    state.onEdit(role)
    expect(state.formData.name).toBe('审查员')
    expect(state.roleNameForSave(role, state.formData.name)).toBe('评审员')
    expect(state.roleNameForSave(role, '安全审查员')).toBe('安全审查员')

    wrapper.unmount()
  })

  it('loads the saved scope before allowing edits instead of overwriting defaults', async () => {
    const wrapper = mountRoleManage()
    await flushPromises()

    const state = setupState(wrapper)
    await state.onSetDataScope(role)
    await flushPromises()

    expect(rbacApi.fetchRoleDataScope).toHaveBeenCalledWith(role.id)
    expect(projectApi.getProjects).toHaveBeenCalledWith({ page: 1, page_size: 200 })
    expect(state.scopeForm.scope_type).toBe('custom')
    expect(state.scopeForm.project_ids).toEqual([22])
    expect(rbacApi.updateRoleDataScope).not.toHaveBeenCalled()

    wrapper.unmount()
  })

  it('closes the dialog and reports an error when the saved scope cannot load', async () => {
    rbacApi.fetchRoleDataScope.mockRejectedValueOnce(new Error('failed'))
    const wrapper = mountRoleManage()
    await flushPromises()

    const state = setupState(wrapper)
    await state.onSetDataScope(role)
    await flushPromises()

    expect(state.scopeDialogVisible).toBe(false)
    expect(messages.error).toHaveBeenCalledWith('数据范围加载失败')

    wrapper.unmount()
  })
})

describe('RoleManage 权限分配失败保护', () => {
  it.each([
    ['权限目录返回空列表', () => rbacApi.listPermissions.mockResolvedValueOnce([])],
    ['权限目录读取失败', () => rbacApi.listPermissions.mockRejectedValueOnce(new Error('network'))],
    ['角色权限读取失败', () => {
      rbacApi.listPermissions.mockResolvedValueOnce([{ id: 1, module: 'project', code: 'project:view', name: '查看项目' }])
      rbacApi.fetchRolePermissions.mockRejectedValueOnce(new Error('network'))
    }],
  ])('当%s时不得把空权限提交成覆盖更新', async (_label, prepareFailure) => {
    rbacApi.fetchRolePermissions.mockReset()
    prepareFailure()
    const wrapper = mountRoleManage()
    await flushPromises()

    const state = setupState(wrapper)
    await state.onAssignPermissions(role).catch(() => undefined)
    expect(state.permissionDataReady).toBe(false)
    state.permTreeRef = {
      getCheckedKeys: vi.fn(() => []),
      getHalfCheckedKeys: vi.fn(() => []),
      setCheckedKeys: vi.fn(),
    }
    await state.onConfirmPermissions()

    expect(rbacApi.assignRolePermissions).not.toHaveBeenCalled()
    expect(messages.error).toHaveBeenCalled()
    wrapper.unmount()
  })
})
