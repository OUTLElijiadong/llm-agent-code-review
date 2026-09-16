import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import AgentStudio from './AgentStudio.vue'
const api = vi.hoisted(() => Object.fromEntries(['listStudioAgents', 'listStudioSkills', 'createStudioAgent', 'createStudioSkill', 'getAgentVersion', 'listAgentVersions', 'bindStudioSkill', 'testStudioAgent', 'submitStudioAgent', 'unbindStudioSkill', 'withdrawStudioAgent'].map(name => [name, vi.fn()])))
vi.mock('@/api/agentStudio', () => api)
vi.mock('element-plus', () => ({ ElMessage: { success: vi.fn(), error: vi.fn(), warning: vi.fn() }, ElMessageBox: { prompt: vi.fn() } }))
const detail = { id: 11, version_number: 1, status: 'draft', checksum: '1234567890', bindings: [], prompt: 'a'.repeat(20), review_focus: 'security', model_config: {} }
beforeEach(() => {
  Object.values(api).forEach(fn => fn.mockReset())
  api.listStudioAgents.mockResolvedValue([]); api.listStudioSkills.mockResolvedValue([])
  api.createStudioAgent.mockResolvedValue({ agent: { id: 1, code: 'draft', name: '草稿' }, version: { id: 11 } })
  api.getAgentVersion.mockImplementation(async () => ({ ...detail, bindings: [] }))
  api.createStudioSkill.mockResolvedValue({ skill: { id: 2 }, version: { id: 21 } })
  api.bindStudioSkill.mockResolvedValue({ id: 31 }); api.testStudioAgent.mockResolvedValue({ status: 'testing' })
})
function render() {
  return mount(AgentStudio, { global: { directives: { loading: () => {} }, stubs: Object.fromEntries(['ElButton', 'ElIcon', 'ElInput', 'ElForm', 'ElFormItem', 'ElSteps', 'ElStep', 'ElSwitch', 'ElSelect', 'ElOption', 'ElInputNumber', 'ElAlert', 'ElDescriptions', 'ElDescriptionsItem'].map(name => [name, { template: '<div><slot/></div>' }])) } })
}
it('创建已成功但详情读取失败，重试不会重复创建', async () => {
  const wrapper = render(); await flushPromises()
  const vm = wrapper.vm as unknown as { skillForm: { enabled: boolean }; persistAndTest: () => Promise<void>; actionError: string }
  vm.skillForm.enabled = false
  api.getAgentVersion.mockRejectedValueOnce(new Error('detail offline'))
  await vm.persistAndTest()
  expect(vm.actionError).toContain('offline')
  await vm.persistAndTest()
  expect(api.createStudioAgent).toHaveBeenCalledTimes(1)
  expect(api.testStudioAgent).toHaveBeenCalledWith(11)
  wrapper.unmount()
})
it('Skill已创建但绑定失败，重试继续绑定且不能跳过绑定直接测试', async () => {
  const wrapper = render(); await flushPromises()
  const vm = wrapper.vm as unknown as { persistAndTest: () => Promise<void> }
  api.bindStudioSkill.mockRejectedValueOnce(new Error('binding offline'))
  await vm.persistAndTest()
  expect(api.testStudioAgent).not.toHaveBeenCalled()
  await vm.persistAndTest()
  expect(api.createStudioSkill).toHaveBeenCalledTimes(1)
  expect(api.bindStudioSkill).toHaveBeenCalledTimes(2)
  expect(api.testStudioAgent).toHaveBeenCalledTimes(1)
  wrapper.unmount()
})
