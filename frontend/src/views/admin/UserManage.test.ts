import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const userApi = vi.hoisted(() => ({
  getUsers: vi.fn(),
  toggleUserStatus: vi.fn(),
  resetPassword: vi.fn(),
  deleteUser: vi.fn(),
}))
const rbacApi = vi.hoisted(() => ({
  listRoles: vi.fn(),
  fetchUserRoles: vi.fn(),
  assignUserRoles: vi.fn(),
}))
const messages = vi.hoisted(() => ({ success: vi.fn(), warning: vi.fn(), error: vi.fn(), info: vi.fn() }))

vi.mock('@/api/user', () => userApi)
vi.mock('@/api/rbac', () => rbacApi)
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: messages }))
vi.mock('element-plus/es/components/message-box/index', () => ({
  ElMessageBox: { confirm: vi.fn() },
}))

import UserManage from './UserManage.vue'

const USERS = [
  {
    id: 42, username: 'someone', nickname: '某人', email: 's@x.dev',
    role: 'user', status: 1, last_login: null, last_login_ip: null,
    create_time: '2026-09-01T00:00:00',
  },
]

const ROLES = [
  { id: 1, code: 'user', name: '普通用户', is_builtin: true },
  { id: 2, code: 'reviewer', name: '评审员', is_builtin: true },
  { id: 3, code: 'auditor', name: '审计员', is_builtin: true },
  { id: 4, code: 'admin', name: '管理员', is_builtin: true },
  { id: 5, code: 'super_admin', name: '超级管理员', is_builtin: true },
  { id: 9, code: 'qa_temp', name: '临时验收角色', is_builtin: false },
]

function mountUserManage(): VueWrapper {
  return mount(UserManage, {
    global: {
      stubs: {
        'el-card': { template: '<div><slot /></div>' },
        'el-tag': { template: '<span class="el-tag-stub"><slot /></span>' },
        'el-input': true,
        'el-select': true,
        'el-option': true,
        'el-button': { template: '<button><slot /></button>' },
        'el-pagination': true,
        'el-dialog': { template: '<div v-if="modelValue" class="el-dialog-stub"><slot /><slot name="footer" /></div>', props: ['modelValue'] },
        'el-form': { template: '<form><slot /></form>' },
        'el-form-item': { template: '<div class="el-form-item-stub"><slot /></div>' },
        'el-radio-group': { template: '<div class="el-radio-group-stub"><slot /></div>' },
        'el-radio': { template: '<label class="el-radio-stub"><slot /></label>' },
        'el-checkbox-group': { template: '<div class="el-checkbox-group-stub"><slot /></div>' },
        'el-checkbox': { template: '<label class="el-checkbox-stub"><slot /></label>' },
        'el-alert': { template: '<div class="el-alert-stub" :data-title="title"></div>', props: ['title'] },
        'el-tooltip': { template: '<span><slot /></span>' },
        EmptyState: true,
      },
    },
  })
}

beforeEach(() => {
  userApi.getUsers.mockReset().mockResolvedValue({ items: USERS, total: 1 })
  userApi.toggleUserStatus.mockReset()
  userApi.resetPassword.mockReset()
  userApi.deleteUser.mockReset()
  rbacApi.listRoles.mockReset().mockResolvedValue(ROLES)
  rbacApi.fetchUserRoles.mockReset().mockResolvedValue([
    { id: 1, code: 'user', name: '普通用户' },
    { id: 9, code: 'qa_temp', name: '临时验收角色' },
  ])
  rbacApi.assignUserRoles.mockReset().mockResolvedValue([])
})

describe('UserManage 统一角色编辑(合并原用户角色分配页)', () => {
  it('卡片同时展示基础角色与 RBAC 附加角色标签', async () => {
    const wrapper = mountUserManage()
    await flushPromises()
    await flushPromises()

    expect(rbacApi.fetchUserRoles).toHaveBeenCalledWith(42)
    const text = wrapper.text()
    expect(text).toContain('普通用户')
    expect(text).toContain('临时验收角色')
    wrapper.unmount()
  })

  it('编辑角色弹窗:基础角色单选 + 附加角色多选(审计员与自定义角色),保存一次写全量', async () => {
    const wrapper = mountUserManage()
    await flushPromises()
    await flushPromises()

    const editBtn = wrapper.findAll('button').find((b) => b.text().includes('编辑角色'))
    expect(editBtn).toBeTruthy()
    await editBtn!.trigger('click')
    await flushPromises()

    // 附加角色预选来自 RBAC(临时验收角色已勾选),超级管理员不出现在任何选项里
    const vm = wrapper.vm as unknown as {
      selectedRole: string
      selectedExtraRoleIds: number[]
      extraRoleOptions: Array<{ id: number; code: string }>
    }
    expect(vm.selectedRole).toBe('user')
    expect(vm.selectedExtraRoleIds).toEqual([9])
    expect(vm.extraRoleOptions.map((r) => r.code)).toEqual(['auditor', 'qa_temp'])

    // 模拟用户改选:基础角色=审查员,附加保留 qa_temp 并加 auditor
    vm.selectedRole = 'reviewer'
    vm.selectedExtraRoleIds = [3, 9]
    const confirmBtn = wrapper.findAll('.el-dialog-stub button').find((b) => b.text().includes('确定'))
    expect(confirmBtn).toBeTruthy()
    await confirmBtn!.trigger('click')
    await flushPromises()

    expect(rbacApi.assignUserRoles).toHaveBeenCalledWith(42, {
      user_id: 42,
      role_ids: [2, 3, 9],
    })
    wrapper.unmount()
  })
})
