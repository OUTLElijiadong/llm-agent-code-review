import { flushPromises, shallowMount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const llmApi = vi.hoisted(() => ({
  fetchLlmModels: vi.fn(),
  getLlmConfig: vi.fn(),
  testLlmConfig: vi.fn(),
  updateLlmConfig: vi.fn(),
  getModelRegistry: vi.fn(),
  saveModelRegistry: vi.fn(),
  saveModelAssignments: vi.fn(),
  syncModelRegistry: vi.fn(),
}))
const messages = vi.hoisted(() => ({
  success: vi.fn(),
  warning: vi.fn(),
}))

vi.mock('@/api/llmConfig', () => llmApi)
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: messages }))
vi.mock('element-plus/es/components/message-box/index', () => ({
  ElMessageBox: { confirm: vi.fn().mockResolvedValue(true) },
}))

import LlmConfig from './LlmConfig.vue'

const config = {
  provider: 'custom',
  base_url: 'https://api.example.com/v1',
  model: 'manual-model',
  active: true,
  api_key_masked: 'sk-****cret',
  is_set: true,
  source: 'global',
  fallback_reason: '',
  timeout_seconds: 60,
  max_retries: 2,
  temperature: 0.2,
}

const GenericStub = { template: '<div><slot /></div>' }

function mountConfig() {
  return shallowMount(LlmConfig, {
    global: {
      stubs: {
        'el-alert': GenericStub,
        'el-divider': GenericStub,
        'el-button': GenericStub,
        'el-card': GenericStub,
        'el-form': GenericStub,
        'el-form-item': GenericStub,
        'el-input': GenericStub,
        'el-input-number': GenericStub,
        'el-option': GenericStub,
        'el-select': GenericStub,
        'el-switch': GenericStub,
        'el-tag': GenericStub,
        'el-tooltip': GenericStub,
      },
      directives: { loading: {} },
    },
  })
}

beforeEach(() => {
  llmApi.getModelRegistry.mockResolvedValue({ models: [], assignments: {}, roles: { chat: '小菱对话', chat_vision: '小菱视觉', orchestrator: '总调度', subagent: '子Agent' } })
  vi.clearAllMocks()
  llmApi.getLlmConfig.mockResolvedValue({ ...config })
})

describe('LlmConfig recoverable interactions', () => {
  it('加载失败后保留可编辑表单并允许重试', async () => {
    llmApi.getLlmConfig.mockRejectedValueOnce(new Error('服务暂时不可用'))
    const wrapper = mountConfig()
    await flushPromises()

    const vm = wrapper.vm as any
    expect(vm.loadError).toBe('服务暂时不可用')
    expect(vm.form.base_url).toBe('https://api.deepseek.com')

    llmApi.getLlmConfig.mockResolvedValueOnce({ ...config })
    await vm.load()
    expect(vm.loadError).toBe('')
    expect(vm.form.model).toBe('manual-model')
    wrapper.unmount()
  })

  it('上游不支持模型列表时保留手工模型和操作建议', async () => {
    llmApi.fetchLlmModels.mockResolvedValue({
      success: true,
      message: '上游不支持模型列表，已保留手工模型',
      models: ['manual-model'],
      selected_model: 'manual-model',
      duration_ms: 10,
      attempts: 1,
      fallback: true,
      retryable: false,
      next_action: '确认后继续测试',
    })
    const wrapper = mountConfig()
    await flushPromises()

    const vm = wrapper.vm as any
    await vm.pullModels()

    expect(llmApi.fetchLlmModels).toHaveBeenCalledWith(expect.objectContaining({
      base_url: config.base_url,
      model: config.model,
      timeout_seconds: 60,
      max_retries: 2,
      temperature: 0.2,
    }))
    expect(llmApi.fetchLlmModels.mock.calls[0][0].api_key).toBeUndefined()
    expect(llmApi.fetchLlmModels.mock.calls[0][0].api_key).not.toBe(config.api_key_masked)
    expect(vm.form.model).toBe('manual-model')
    expect(vm.operation.type).toBe('warning')
    expect(vm.operation.description).toBe('确认后继续测试')
    wrapper.unmount()
  })

  it('停用的历史覆盖仍可恢复系统默认并清除旧 Key', async () => {
    const inactive = {
      ...config,
      active: false,
      source: 'default',
      fallback_reason: 'inactive',
    }
    llmApi.getLlmConfig.mockResolvedValueOnce(inactive)
    llmApi.updateLlmConfig.mockResolvedValueOnce({
      ...inactive,
      fallback_reason: '',
    })
    const wrapper = mountConfig()
    await flushPromises()

    const vm = wrapper.vm as any
    expect(vm.canRestoreDefault).toBe(true)
    await vm.restoreDefault()

    expect(llmApi.updateLlmConfig).toHaveBeenCalledWith({ active: false, api_key: '' })
    wrapper.unmount()
  })

  it('保存失败时不丢失未保存的 Key 和表单内容', async () => {
    llmApi.updateLlmConfig.mockRejectedValue(new Error('数据库未提交'))
    const wrapper = mountConfig()
    await flushPromises()

    const vm = wrapper.vm as any
    vm.form.api_key = 'sk-unsaved'
    vm.form.model = 'edited-model'
    await vm.save()

    expect(vm.form.api_key).toBe('sk-unsaved')
    expect(vm.form.model).toBe('edited-model')
    expect(vm.operation.type).toBe('error')
    expect(vm.operation.description).toContain('表单内容已保留')
    wrapper.unmount()
  })

  it('同一端点的完整资源路径可安全复用已保存 Key', async () => {
    llmApi.updateLlmConfig.mockResolvedValue({ ...config })
    const wrapper = mountConfig()
    await flushPromises()

    const vm = wrapper.vm as any
    vm.form.base_url = 'https://api.example.com/v1/chat/completions'
    vm.form.api_key = ''
    await vm.save()

    expect(messages.warning).not.toHaveBeenCalledWith(
      '启用新的全局端点前，请填写该端点的 API Key',
    )
    expect(llmApi.updateLlmConfig).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('系统默认 Key 只能测试默认端点，不能被当作全局覆盖 Key 保存', async () => {
    llmApi.getLlmConfig.mockResolvedValueOnce({
      ...config,
      provider: 'deepseek',
      base_url: 'https://api.deepseek.com',
      model: 'deepseek-v4-flash',
      active: false,
      source: 'default',
      is_set: true,
      fallback_reason: 'credential_unavailable',
    })
    const wrapper = mountConfig()
    await flushPromises()

    const vm = wrapper.vm as any
    expect(vm.form.base_url).toBe('https://api.deepseek.com')
    expect(vm.statusLabel).toContain('已配置')
    vm.form.active = true
    vm.form.api_key = ''
    await vm.save()

    expect(messages.warning).toHaveBeenCalledWith(
      '启用新的全局端点前，请填写该端点的 API Key',
    )
    expect(llmApi.updateLlmConfig).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})


describe('模型注册表恢复', () => {
  it('加载失败禁止空表覆盖，并提供持久错误和重试', async () => {
    llmApi.getModelRegistry.mockRejectedValueOnce(new Error('注册表暂时不可用'))
    const wrapper = mountConfig()
    await flushPromises()
    const vm = wrapper.vm as any
    expect(vm.registryReady).toBe(false)
    expect(vm.registryError).toBe('注册表暂时不可用')
    await vm.persistRegistry()
    await vm.persistAssignments()
    expect(llmApi.saveModelRegistry).not.toHaveBeenCalled()
    expect(llmApi.saveModelAssignments).not.toHaveBeenCalled()
    await vm.loadRegistry()
    expect(vm.registryReady).toBe(true)
    expect(vm.registryError).toBe('')
    wrapper.unmount()
  })

  it('同步失败保留未保存编辑，避免失败回退覆盖用户输入', async () => {
    const wrapper = mountConfig()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.newModelId = 'deepseek-flash'
    vm.addManualModel()
    llmApi.syncModelRegistry.mockResolvedValueOnce({ success: false, message: '上游限流', models: [], added: [], fetched: [] })
    await vm.syncRegistry()
    expect(vm.registry.map((item: any) => item.id)).toEqual(['deepseek-flash'])
    expect(vm.registry[0].vision).toBe(true)
    expect(vm.registryError).toBe('上游限流')
    expect(vm.registrySyncing).toBe(false)
    wrapper.unmount()
  })

  it('未知模型不凭名称自动标记视觉，分配失败保留选择', async () => {
    const wrapper = mountConfig()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.newModelId = 'unknown-vision-model'
    vm.addManualModel()
    expect(vm.registry[0].vision).toBe(false)
    vm.assignments.chat = 'unknown-vision-model'
    llmApi.saveModelAssignments.mockRejectedValueOnce(new Error('模型尚未登记'))
    await vm.persistAssignments()
    expect(vm.assignments.chat).toBe('unknown-vision-model')
    expect(vm.registryError).toBe('模型尚未登记')
    expect(vm.registrySaving).toBe(false)
    wrapper.unmount()
  })
})

it('逐个子 Agent 默认继承，独立覆盖与失效提示可见', async () => {
  llmApi.getModelRegistry.mockResolvedValueOnce({
    models: [{ id: 'worker', label: 'worker', vision: false, source: 'manual', added_at: '' }],
    assignments: { subagent: 'worker', 'agent:code_reviewer': 'worker' },
    roles: { subagent: '子 Agent 默认', 'agent:code_reviewer': '代码审查', 'agent:language_detector': '语言识别' },
    warnings: ['旧分配已失效，请清除'],
  })
  const wrapper = mountConfig()
  await flushPromises()
  const vm = wrapper.vm as any
  expect(vm.childOverrideCount).toBe(1)
  expect(Object.keys(vm.childRoleLabels)).toHaveLength(2)
  expect(wrapper.text()).toContain('继承默认')
  expect(vm.registryWarnings).toEqual(['旧分配已失效，请清除'])
  vm.assignments['agent:code_reviewer'] = ''
  llmApi.saveModelAssignments.mockResolvedValueOnce({ models: vm.registry, assignments: { subagent: 'worker' }, roles: vm.roleLabels, warnings: [] })
  await vm.persistAssignments()
  expect(llmApi.saveModelAssignments).toHaveBeenCalledWith(expect.objectContaining({ 'agent:code_reviewer': '' }))
  expect(vm.childOverrideCount).toBe(0)
  expect(vm.registryWarnings).toEqual([])
  wrapper.unmount()
})
