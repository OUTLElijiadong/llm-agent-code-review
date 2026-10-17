import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import AgentStudio from './AgentStudio.vue'
const api = vi.hoisted(() => Object.fromEntries(['listStudioAgents', 'listStudioSkills', 'createStudioAgent', 'reviseStudioAgent', 'createStudioSkill', 'getAgentVersion', 'listAgentVersions', 'bindStudioSkill', 'testStudioAgent', 'submitStudioAgent', 'unbindStudioSkill', 'withdrawStudioAgent'].map(name => [name, vi.fn()])))
vi.mock('@/api/agentStudio', () => api)
vi.mock('element-plus', () => ({ ElMessage: { success: vi.fn(), error: vi.fn(), warning: vi.fn() }, ElMessageBox: { prompt: vi.fn() } }))
const detail = { id: 11, version_number: 1, status: 'draft', checksum: '1234567890', bindings: [], prompt: 'a'.repeat(20), review_focus: 'security', model_config: {} }
beforeEach(() => {
  Object.values(api).forEach(fn => fn.mockReset())
  api.listStudioAgents.mockResolvedValue([]); api.listStudioSkills.mockResolvedValue([])
  api.createStudioAgent.mockResolvedValue({ agent: { id: 1, code: 'draft', name: '草稿' }, version: { id: 11 } })
  api.reviseStudioAgent.mockResolvedValue({ id: 12, status: 'draft' })
  api.getAgentVersion.mockImplementation(async () => ({ ...detail, bindings: [] }))
  api.createStudioSkill.mockResolvedValue({ skill: { id: 2 }, version: { id: 21 } })
  api.bindStudioSkill.mockResolvedValue({ id: 31 }); api.testStudioAgent.mockResolvedValue({ status: 'testing' })
})

it.each(['published', 'rejected'])('已有 %s 版本必须先创建新草稿，不能复测旧版', async (status) => {
  const asset = { id: 1, code: 'reviewer', name: '鉴权审查员', status }
  api.listStudioAgents.mockResolvedValue([asset])
  api.listAgentVersions.mockResolvedValue([{ id: 11, version_number: 1, status }])
  api.getAgentVersion.mockImplementation(async (id: number) => ({ ...detail, id, status: id === 11 ? status : 'draft', bindings: [] }))
  const wrapper = render(); await flushPromises()
  const vm = wrapper.vm as unknown as {
    resume: (agent: typeof asset) => Promise<void>
    beginRevision: () => void
    persistAndTest: () => Promise<void>
    skillForm: { enabled: boolean }
    agentForm: { prompt: string; review_focus: string }
  }
  await vm.resume(asset)
  vm.skillForm.enabled = false
  await vm.persistAndTest()
  expect(api.testStudioAgent).not.toHaveBeenCalled()

  vm.beginRevision()
  vm.agentForm.prompt = '仅根据给定代码核对资源归属与账号隔离，输出证据充分的问题。'
  vm.agentForm.review_focus = '资源归属与账号隔离'
  await vm.persistAndTest()
  expect(api.reviseStudioAgent).toHaveBeenCalledWith(1, expect.objectContaining({ prompt: vm.agentForm.prompt }))
  expect(api.createStudioAgent).not.toHaveBeenCalled()
  expect(api.testStudioAgent).toHaveBeenCalledWith(12)
  wrapper.unmount()
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
