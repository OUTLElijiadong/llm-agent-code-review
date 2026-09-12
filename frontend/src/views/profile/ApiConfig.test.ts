import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  getApiConfig: vi.fn(), saveApiConfig: vi.fn(), deleteApiConfig: vi.fn(), testApiConnection: vi.fn(),
  confirm: vi.fn(), success: vi.fn(), error: vi.fn(), warning: vi.fn(),
}))
vi.mock('@/api/apiConfig', () => mocks)
vi.mock('element-plus/es/components/message-box/index', () => ({ ElMessageBox: { confirm: mocks.confirm } }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: mocks }))
import ApiConfig from './ApiConfig.vue'

const wrappers: VueWrapper[] = []
const saved = (model = 'deepseek-chat', provider = 'deepseek') => ({
  provider, model, api_key_masked: 'sk-***1234', base_url: 'https://api.deepseek.com', is_custom: true, is_active: true,
})
function render() {
  const wrapper = mount(ApiConfig, { global: { stubs: {
    'el-card': { template: '<div><slot /></div>' },
    'el-tag': { template: '<span><slot /></span>' },
    'el-divider': true,
    'el-form': { methods: { validate: () => Promise.resolve(true), validateField: () => Promise.resolve(true) }, template: '<form><slot /></form>' },
    'el-form-item': { template: '<div><slot /></div>' },
    'el-select': { props: ['modelValue'], template: '<select :value="modelValue"><slot /></select>' },
    'el-option': { props: ['value', 'label'], template: '<option :value="value">{{ label }}</option>' },
    'el-input': { props: ['modelValue', 'placeholder', 'type'], emits: ['update:modelValue'], template: '<input :value="modelValue" :placeholder="placeholder" :type="type" @input="$emit(\'update:modelValue\', $event.target.value)" />' },
    'el-button': { props: ['loading', 'disabled'], template: '<button type="button" :disabled="loading || disabled"><slot /></button>' },
    'el-collapse': { template: '<div><slot /></div>' },
    'el-collapse-item': { template: '<div><slot /></div>' },
    'el-alert': { props: ['title'], template: '<div>{{ title }}<slot /></div>' },
  } } })
  wrappers.push(wrapper)
  return wrapper
}
const modelInput = (wrapper: VueWrapper) => wrapper.findAll('input')[2]
async function click(wrapper: VueWrapper, label: string) {
  await wrapper.findAll('button').find(button => button.text().includes(label))!.trigger('click')
  await flushPromises()
}
beforeEach(() => {
  Object.values(mocks).forEach(mock => mock.mockReset())
  mocks.getApiConfig.mockResolvedValue({ ...saved(), is_custom: false, api_key_masked: '' })
  mocks.confirm.mockResolvedValue('confirm')
  mocks.deleteApiConfig.mockResolvedValue('ok')
  mocks.saveApiConfig.mockImplementation(async payload => ({ ...saved(), ...payload }))
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()) })

describe('个人 API 模型默认值', () => {
  it('新建使用最新 Flash 建议，即使平台返回旧默认名也不回填为新建模型', async () => {
    const wrapper = render()
    await flushPromises()
    expect(modelInput(wrapper).element.value).toBe('deepseek-flash')
    expect(modelInput(wrapper).attributes('placeholder')).toBe('deepseek-flash')
    expect(wrapper.text()).toContain('推荐 deepseek-flash')
    expect(wrapper.text()).toContain('deepseek-v4-pro')
    expect(wrapper.text()).not.toContain('推荐 deepseek-chat')
    expect(mocks.saveApiConfig).not.toHaveBeenCalled()
  })

  it.each(['deepseek-chat', 'deepseek-reasoner', 'deepseek-v4-pro', 'my-private-model'])('已保存模型 %s 加载与保存时保持原值', async (model) => {
    mocks.getApiConfig.mockResolvedValue(saved(model))
    const wrapper = render()
    await flushPromises()
    expect(modelInput(wrapper).element.value).toBe(model)
    await wrapper.findAll('input')[0].setValue('sk-test-only')
    await click(wrapper, '保存配置')
    expect(mocks.saveApiConfig).toHaveBeenCalledWith(expect.objectContaining({ model }))
    expect(modelInput(wrapper).element.value).toBe(model)
  })

  it('手动填写任意模型后按原值测试连接', async () => {
    const wrapper = render()
    await flushPromises()
    await wrapper.findAll('input')[0].setValue('sk-test-only')
    await modelInput(wrapper).setValue('my-custom-model')
    mocks.testApiConnection.mockResolvedValue({ success: true, model: 'my-custom-model', message: 'ok', duration_ms: 1 })
    await click(wrapper, '测试连接')
    expect(mocks.testApiConnection).toHaveBeenCalledWith(expect.objectContaining({ model: 'my-custom-model' }))
  })

  it('确认并成功重置后，新建表单恢复 Flash，未自动保存新配置', async () => {
    mocks.getApiConfig.mockResolvedValue(saved('deepseek-reasoner'))
    const wrapper = render()
    await flushPromises()
    await click(wrapper, '恢复系统默认')
    expect(mocks.deleteApiConfig).toHaveBeenCalledOnce()
    expect(modelInput(wrapper).element.value).toBe('deepseek-flash')
    expect(mocks.saveApiConfig).not.toHaveBeenCalled()
  })

  it('重置失败保留已保存模型并反馈错误', async () => {
    mocks.getApiConfig.mockResolvedValue(saved('private-model', 'custom'))
    mocks.deleteApiConfig.mockRejectedValue(new Error('删除失败'))
    const wrapper = render()
    await flushPromises()
    await click(wrapper, '恢复系统默认')
    expect(modelInput(wrapper).element.value).toBe('private-model')
    expect(mocks.error).toHaveBeenCalledWith('删除失败')
  })
})

describe('配置读取失败反馈', () => {
  it('初始读取中不假称系统默认，禁用编辑与保存', async () => {
    mocks.getApiConfig.mockReturnValue(new Promise(() => {}))
    const wrapper = render()
    expect(wrapper.get('[role="status"]').text()).toContain('正在读取当前 API 配置')
    expect(wrapper.find('.current-status').exists()).toBe(false)
    expect(modelInput(wrapper).element.disabled).toBe(true)
    expect(wrapper.findAll('button').find(button => button.text().includes('保存配置'))!.element.disabled).toBe(true)
  })

  it('真实读取失败后常驻重试，成功读取旧配置前不能保存；未编辑时回填原模型', async () => {
    mocks.getApiConfig.mockRejectedValueOnce(new Error('network')).mockResolvedValue(saved('old-saved-model'))
    const wrapper = render()
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('未能读取当前 API 配置')
    expect(wrapper.find('.current-status').exists()).toBe(false)
    await click(wrapper, '保存配置')
    expect(mocks.saveApiConfig).not.toHaveBeenCalled()
    expect(mocks.deleteApiConfig).not.toHaveBeenCalled()
    await click(wrapper, '重新读取')
    expect(mocks.getApiConfig).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(modelInput(wrapper).element.value).toBe('old-saved-model')
    expect(wrapper.findAll('button').find(button => button.text().includes('保存配置'))!.element.disabled).toBe(false)
  })

  it('读取失败期间手输草稿可测试，重试成功也不覆盖草稿', async () => {
    mocks.getApiConfig.mockRejectedValueOnce(new Error('network')).mockResolvedValue(saved('remote-original'))
    const wrapper = render()
    await flushPromises()
    await wrapper.findAll('input')[0].setValue('sk-local-draft')
    await wrapper.findAll('input')[1].setValue('https://custom.example/v1')
    await modelInput(wrapper).setValue('draft-model')
    expect(wrapper.get('[role="alert"]').text()).toContain('仅可填写并测试连接')
    mocks.testApiConnection.mockResolvedValue({ success: true, model: 'draft-model', message: 'ok', duration_ms: 1 })
    await click(wrapper, '测试连接')
    expect(mocks.testApiConnection).toHaveBeenCalledWith(expect.objectContaining({ model: 'draft-model' }))
    await click(wrapper, '重新读取')
    expect(modelInput(wrapper).element.value).toBe('draft-model')
    expect(wrapper.findAll('input')[0].element.value).toBe('sk-local-draft')
    expect(wrapper.findAll('input')[1].element.value).toBe('https://custom.example/v1')
    expect(wrapper.find('.current-status').text()).toContain('remote-original')
    expect(mocks.saveApiConfig).not.toHaveBeenCalled()
  })
})
