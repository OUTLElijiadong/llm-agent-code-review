import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import XiaolingPreferenceDialog from './XiaolingPreferenceDialog.vue'
const mocks = vi.hoisted(() => ({ getProfile: vi.fn(), updateProfile: vi.fn(), markPreferencePrompted: vi.fn(), success: vi.fn() }))
vi.mock('@/api/profile', () => mocks)
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { success: mocks.success } }))
const saved = { user_id: 1, hobbies: '摄影,爬山', goals: '已有目标', tech_stack: 'Elixir,Python', preferred_language: '中文', experience_level: 'advanced', focus_areas: ['自定义方向'], auto_learn: false, derived_summary: '已有学习' }
function render() { return mount(XiaolingPreferenceDialog, { props: { modelValue: true }, global: { stubs: {
  PrismMascot: true,
  ElDialog: { template: '<div><slot/><slot name="footer"/></div>' },
  ElButton: { props: ['disabled'], template: '<button :disabled="disabled"><slot/></button>' },
} } }) }
beforeEach(() => {
  mocks.getProfile.mockResolvedValue(saved)
  mocks.updateProfile.mockResolvedValue(saved)
  mocks.markPreferencePrompted.mockResolvedValue(saved)
})
it('读取已有偏好后只改目标，保持自由兴趣与技术栈并原子结算', async () => {
  const wrapper = render()
  await flushPromises()
  expect((wrapper.get('#pref-hobby').element as HTMLInputElement).value).toBe(saved.hobbies)
  await wrapper.get('#pref-goal').setValue('新目标')
  await wrapper.findAll('button').find(button => button.text() === '保存偏好')!.trigger('click')
  await flushPromises()
  expect(mocks.updateProfile).toHaveBeenCalledWith(expect.objectContaining({ hobbies: saved.hobbies, tech_stack: saved.tech_stack, goals: '新目标', preference_prompted: 1 }))
  expect(mocks.markPreferencePrompted).not.toHaveBeenCalled()
  wrapper.unmount()
})
it('读取失败不能覆盖旧偏好，保存失败保留输入可重试', async () => {
  mocks.getProfile.mockRejectedValueOnce(new Error('offline'))
  const wrapper = render()
  await flushPromises()
  expect(wrapper.get('fieldset').attributes('disabled')).toBeDefined()
  expect(wrapper.get('[role=alert]').text()).toContain('读取失败')
  await wrapper.findAll('button').find(button => button.text() === '重新读取')!.trigger('click')
  await flushPromises()
  await wrapper.get('#pref-hobby').setValue('新兴趣')
  mocks.updateProfile.mockRejectedValueOnce(new Error('offline'))
  const save = wrapper.findAll('button').find(button => button.text() === '保存偏好')!
  await save.trigger('click'); await flushPromises()
  expect(wrapper.get('[role=alert]').text()).toContain('保存失败')
  expect((wrapper.get('#pref-hobby').element as HTMLInputElement).value).toBe('新兴趣')
  await save.trigger('click'); await flushPromises()
  expect(wrapper.emitted('update:modelValue')).toContainEqual([false])
  wrapper.unmount()
})
