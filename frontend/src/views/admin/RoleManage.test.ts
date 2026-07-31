import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const rbacApi = vi.hoisted(() => ({
  listRoles: vi.fn(),
  createRole: vi.fn(),
  updateRole: vi.fn(),
  deleteRole: vi.fn(),
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
vi.mock('element-plus/es/components/message-box/index', () => ({
  ElMessageBox: { confirm: vi.fn() },
}))
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
