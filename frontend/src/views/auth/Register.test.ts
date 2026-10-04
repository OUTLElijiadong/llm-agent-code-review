import ElementPlus from 'element-plus'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const userStore = vi.hoisted(() => ({ register: vi.fn() }))
const router = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }))
const authApi = vi.hoisted(() => ({ getCaptcha: vi.fn() }))
const messages = vi.hoisted(() => ({ success: vi.fn() }))

vi.mock('@/stores/user', () => ({ useUserStore: () => userStore }))
vi.mock('vue-router', () => ({ useRouter: () => router }))
vi.mock('@/api/auth', () => authApi)
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: messages }))

import Register from './Register.vue'

async function mountRegister(email: string) {
  const wrapper = mount(Register, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  await wrapper.find('input[autocomplete="username"]').setValue('tester26')
  const passwords = wrapper.findAll('input[autocomplete="new-password"]')
  await passwords[0].setValue('correct horse battery staple')
  await passwords[1].setValue('correct horse battery staple')
  await wrapper.find('input[autocomplete="email"]').setValue(email)
  await wrapper.find('input[autocomplete="off"]').setValue('4')
  await wrapper.find('.btn-register').trigger('click')
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  authApi.getCaptcha.mockResolvedValue({
    captcha_id: 'captcha-1',
    question: '2 + 2 = ?',
    image_data: 'data:image/png;base64,aGVsbG8=',
    beta_registration_enabled: false,
  })
  userStore.register.mockResolvedValue(undefined)
})

describe('Register 选填邮箱', () => {
  it('显示位图挑战，题目或字符不作为可复制文本展示', async () => {
    const wrapper = mount(Register, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(wrapper.find('img[alt="注册验证码图片"]').attributes('src')).toBe('data:image/png;base64,aGVsbG8=')
    expect(wrapper.text()).not.toContain('2 + 2 = ?')
    wrapper.unmount()
  })

  it('挑战加载失败时禁用提交并提供刷新入口', async () => {
    authApi.getCaptcha.mockRejectedValueOnce(new Error('unavailable'))
    const wrapper = mount(Register, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(wrapper.find('.btn-register').attributes('disabled')).toBeDefined()
    expect(wrapper.find('button[aria-label="刷新验证码"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('图片解码失败可刷新恢复，失败期间不提交注册', async () => {
    const wrapper = mount(Register, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper.get('img[alt="注册验证码图片"]').trigger('error')
    expect(wrapper.find('.btn-register').attributes('disabled')).toBeDefined()
    await wrapper.find('button[aria-label="刷新验证码"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('img[alt="注册验证码图片"]').exists()).toBe(true)
    expect(wrapper.find('.btn-register').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('非PNG数据地址不会渲染也不能提交', async () => {
    authApi.getCaptcha.mockResolvedValueOnce({ captcha_id: 'bad-image', question: 'hidden', image_data: 'data:image/svg+xml,<svg/>', beta_registration_enabled: false })
    const wrapper = mount(Register, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.find('.btn-register').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })
  it('把纯空白邮箱当作未填，不阻断注册也不向 API 发空白串', async () => {
    const wrapper = await mountRegister('   ')

    expect(userStore.register).toHaveBeenCalledWith(expect.objectContaining({ email: undefined }))
    expect(wrapper.text()).not.toContain('请输入有效的邮箱地址')
    wrapper.unmount()
  })

  it('去除有效邮箱两端空格后再提交', async () => {
    const wrapper = await mountRegister('  tester@example.com  ')

    expect(userStore.register).toHaveBeenCalledWith(
      expect.objectContaining({ email: 'tester@example.com' }),
    )
    wrapper.unmount()
  })

  it('保留 Element Plus 原生邮箱类型校验，并在校验前归一化空格', async () => {
    const wrapper = mount(Register, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const state = (wrapper.vm as unknown as { $: { setupState: Record<string, any> } }).$.setupState
    const emailRule = state.rules.email[0]

    expect(emailRule.type).toBe('email')
    expect(emailRule.transform('   ')).toBe('')
    expect(emailRule.transform('  tester@example.com  ')).toBe('tester@example.com')
    wrapper.unmount()
  })

  it('统一账号标签，不暴露供应商在线状态或伪造版本号', async () => {
    const wrapper = mount(Register, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(wrapper.text()).toContain('账号')
    expect(wrapper.text()).not.toContain('账号 / 学号')
    expect(wrapper.text()).not.toContain('DeepSeek V4 在线')
    expect(wrapper.text()).not.toContain('v1.0 · 2026')
    wrapper.unmount()
  })
})
