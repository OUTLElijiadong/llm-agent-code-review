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
  { id: 4, code: 'admin', name: '管理员', is_builtin: true },
  { id: 5, code: 'super_admin', name: '超级管理员', is_builtin: true },
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
  rbacApi.assignUserRoles.mockReset().mockResolvedValue([])
})

describe('UserManage 统一角色编辑(合并原用户角色分配页)', () => {
  it('卡片只展示唯一基础角色，不再查询或展示附加角色', async () => {
    const wrapper = mountUserManage()
    await flushPromises()
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('普通用户')
    expect(text).not.toContain('临时验收角色')
    wrapper.unmount()
  })

  it('编辑角色弹窗只允许选择一个基础角色并覆盖保存', async () => {
    const wrapper = mountUserManage()
    await flushPromises()
    await flushPromises()

    const editBtn = wrapper.findAll('button').find((b) => b.text().includes('编辑角色'))
    expect(editBtn).toBeTruthy()
    await editBtn!.trigger('click')
    await flushPromises()

    const vm = wrapper.vm as unknown as {
      selectedRole: string
    }
    expect(vm.selectedRole).toBe('user')

    // 模拟用户改选基础角色为评审员。
    vm.selectedRole = 'reviewer'
    const confirmBtn = wrapper.findAll('.el-dialog-stub button').find((b) => b.text().includes('确定'))
    expect(confirmBtn).toBeTruthy()
    await confirmBtn!.trigger('click')
    await flushPromises()

    expect(rbacApi.assignUserRoles).toHaveBeenCalledWith(42, {
      user_id: 42,
      role_ids: [2],
    })
    wrapper.unmount()
  })
})
