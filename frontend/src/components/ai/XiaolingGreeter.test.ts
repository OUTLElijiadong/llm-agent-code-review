import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import XiaolingGreeter from './XiaolingGreeter.vue'
const state = vi.hoisted(() => ({ getProfile: vi.fn(), admin: false }))
vi.mock('@/api/profile', () => ({ getProfile: state.getProfile }))
vi.mock('@/stores/user', () => ({ useUserStore: () => ({ profile: { id: 1, role: 'user' }, isAdmin: () => state.admin }) }))
const data = { hobbies: '', goals: '', tech_stack: '', preferred_language: '', experience_level: '', focus_areas: [], preference_prompted: 0, should_prompt: true }
function render() { return mount(XiaolingGreeter, { global: { stubs: { XiaolingOnboardingDialog: true, XiaolingPreferenceDialog: true } } }) }
beforeEach(() => { sessionStorage.clear(); localStorage.clear(); state.admin = false; state.getProfile.mockResolvedValue(data) })
it('已有偏好或经常使用者不主动弹出，仍可手动打开', async () => {
  state.getProfile.mockResolvedValueOnce({ ...data, hobbies: '摄影' })
  const first = render(); await flushPromises()
  expect(first.findComponent({ name: 'XiaolingPreferenceDialog' }).props('modelValue')).toBe(false)
  first.unmount()
  state.getProfile.mockResolvedValueOnce({ ...data, should_prompt: false })
  const second = render(); await flushPromises()
  expect(second.findComponent({ name: 'XiaolingPreferenceDialog' }).props('modelValue')).toBe(false)
  window.dispatchEvent(new Event('prism:open-preference-dialog')); await flushPromises()
  expect(second.findComponent({ name: 'XiaolingPreferenceDialog' }).props('modelValue')).toBe(true)
  second.unmount()
})
it('无偏好新用户会询问；关闭后七天不再打扰；管理员不主动问', async () => {
  const first = render(); await flushPromises()
  const dialog = first.findComponent({ name: 'XiaolingPreferenceDialog' })
  expect(dialog.props('modelValue')).toBe(true)
  dialog.vm.$emit('closed'); first.unmount()
  const next = render(); await flushPromises()
  expect(next.findComponent({ name: 'XiaolingPreferenceDialog' }).props('modelValue')).toBe(false)
  next.unmount(); localStorage.clear(); state.admin = true
  const admin = render(); await flushPromises()
  expect(admin.findComponent({ name: 'XiaolingPreferenceDialog' }).props('modelValue')).toBe(false)
  admin.unmount()
})
