import { flushPromises, shallowMount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const llmApi = vi.hoisted(() => ({
  fetchLlmModels: vi.fn(),
  getLlmConfig: vi.fn(),
  testLlmConfig: vi.fn(),
  updateLlmConfig: vi.fn(),
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
    expect(vm.form.model).toBe('manual-model')
    expect(vm.operation.type).toBe('warning')
    expect(vm.operation.description).toBe('确认后继续测试')
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
})
