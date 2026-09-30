import { shallowMount, type VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const router = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() }))
const authApi = vi.hoisted(() => ({ changePassword: vi.fn() }))
const messages = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }))
const session = vi.hoisted(() => ({ clearSession: vi.fn() }))

vi.mock('vue-router', () => ({ useRouter: () => router }))
vi.mock('@/api/auth', () => authApi)
vi.mock('@/stores/user', () => ({ useUserStore: () => session }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: messages }))

import ChangePassword from './ChangePassword.vue'

function setupState(wrapper: VueWrapper): Record<string, any> {
  return (wrapper.vm as unknown as { $: { setupState: Record<string, any> } }).$.setupState
}

beforeEach(() => {
  authApi.changePassword.mockResolvedValue(undefined)
  session.clearSession.mockReset()
  router.replace.mockReset()
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

  it('改密成功先清除已失效的本地会话，再替换到登录页', async () => {
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
    state.form.oldPassword = 'existing-legacy-password'
    state.form.newPassword = 'correct horse battery staple'
    state.formRef = {
      validate: async (callback: (valid: boolean) => Promise<void>) => callback(true),
    }

    await state.handleSubmit()

    expect(authApi.changePassword).toHaveBeenCalledWith({
      old_password: 'existing-legacy-password',
      new_password: 'correct horse battery staple',
    })
    expect(session.clearSession).toHaveBeenCalledOnce()
    expect(router.replace).toHaveBeenCalledWith('/login')
    expect(session.clearSession.mock.invocationCallOrder[0]).toBeLessThan(router.replace.mock.invocationCallOrder[0])
    wrapper.unmount()
  })
})
