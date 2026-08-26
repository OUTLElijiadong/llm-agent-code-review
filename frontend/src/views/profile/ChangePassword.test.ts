import { shallowMount, type VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const router = vi.hoisted(() => ({ push: vi.fn(), back: vi.fn() }))
const authApi = vi.hoisted(() => ({ changePassword: vi.fn() }))
const messages = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }))

vi.mock('vue-router', () => ({ useRouter: () => router }))
vi.mock('@/api/auth', () => authApi)
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: messages }))

import ChangePassword from './ChangePassword.vue'

function setupState(wrapper: VueWrapper): Record<string, any> {
  return (wrapper.vm as unknown as { $: { setupState: Record<string, any> } }).$.setupState
}

beforeEach(() => {
  authApi.changePassword.mockResolvedValue(undefined)
})

describe('ChangePassword 空提交回归', () => {
  it('一次展示三个必填提示且不调用修改密码接口', async () => {
    const wrapper = shallowMount(ChangePassword, {
      global: {
        stubs: {
          ArrowLeft: true,
          'el-button': { template: '<button><slot /></button>' },
          'el-form': { template: '<form><slot /></form>' },
          'el-form-item': { template: '<div><slot /></div>' },
          'el-icon': { template: '<i><slot /></i>' },
          'el-input': true,
        },
      },
    })
    const state = setupState(wrapper)
    expect([
      state.rules.oldPassword[0].message,
      state.rules.newPassword[0].message,
      state.rules.confirmPassword[0].message,
    ]).toEqual([
      '请输入旧密码',
      '请输入新密码',
      '请确认新密码',
    ])

    const validate = vi.fn(async (callback: (valid: boolean) => Promise<void>) => callback(false))
    state.formRef = { validate }
    await state.handleSubmit()

    expect(validate).toHaveBeenCalledOnce()
    expect(authApi.changePassword).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
