import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import FeedbackCenter from './FeedbackCenter.vue'
import MaintenanceCenter from './MaintenanceCenter.vue'
const api = vi.hoisted(() => ({ list: vi.fn(), create: vi.fn(), update: vi.fn(), close: vi.fn(), confirm: vi.fn(), admin: false }))
vi.mock('@/api/feedback', () => ({ getFeedbackList: api.list, createFeedback: api.create, replyFeedback: api.update }))
vi.mock('@/api/maintenance', () => ({ getTickets: api.list, createTicket: api.create, handleTicket: api.update, closeTicket: api.close }))
vi.mock('@/stores/user', () => ({ useUserStore: () => ({ isAdmin: () => api.admin }) }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { success: vi.fn(), warning: vi.fn() } }))
vi.mock('element-plus/es/components/message-box/index', () => ({ ElMessageBox: { confirm: api.confirm } }))
beforeEach(() => { vi.clearAllMocks(); api.admin = false; api.list.mockResolvedValue({ items: [], total: 0 }) })
function render(component: typeof FeedbackCenter | typeof MaintenanceCenter) {
  return mount(component, { global: { directives: { loading: () => {} }, stubs: {
    ElCard: { template: '<section><slot/></section>' },
    ElButton: { props: ['loading', 'disabled'], template: '<button :disabled="loading || disabled"><slot/></button>' },
    ElAlert: { props: ['title'], template: '<section role="alert">{{ title }}<slot/></section>' },
    ElEmpty: { props: ['description'], template: '<p>{{ description }}</p>' },
    ElDialog: { props: ['modelValue'], template: '<aside v-if="modelValue"><slot/><slot name="footer"/></aside>' },
    ElForm: { template: '<form><slot/></form>' }, ElFormItem: { template: '<label><slot/></label>' },
    ElInput: { props: ['modelValue'], template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />' },
    ElDescriptions: { template: '<div><slot/></div>' }, ElDescriptionsItem: { template: '<div><slot/></div>' },
    ElRadioGroup: true, ElRadioButton: true,
    ElSelect: true, ElOption: true, ElPagination: true, ElTag: { template: '<span><slot/></span>' },
  } } })
}
for (const [name, component] of [['反馈', FeedbackCenter], ['工单', MaintenanceCenter]] as const) {
  it(`${name}失败读取有常驻重试且不伪造空态`, async () => {
    api.list.mockRejectedValueOnce(new Error('连接失败'))
    const wrapper = render(component); await flushPromises()
    expect(wrapper.text()).toContain('连接失败')
    expect(wrapper.text()).not.toContain(`暂无${name}`)
    await wrapper.findAll('button').find(b => b.text() === '重新加载')!.trigger('click'); await flushPromises()
    expect(api.list).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).not.toContain('连接失败')
    expect(wrapper.text()).toContain(`暂无${name}`)
    wrapper.unmount()
  })
  it(`${name}提交失败保留内容和表单`, async () => {
    api.create.mockRejectedValueOnce(new Error('服务暂不可用'))
    const wrapper = render(component); await flushPromises()
    await wrapper.findAll('button').find(b => b.text().startsWith('提交'))!.trigger('click')
    for (const input of wrapper.findAll('input')) await input.setValue('隔离测试内容')
    await wrapper.findAll('button').find(b => b.text() === '提交')!.trigger('click'); await flushPromises()
    expect(api.create).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('服务暂不可用')
    expect(wrapper.find('aside').exists()).toBe(true)
    expect(wrapper.findAll('input').every(i => (i.element as HTMLInputElement).value === '隔离测试内容')).toBe(true)
    wrapper.unmount()
  })
}
it('关闭工单取消不调用接口，接口失败保留原状态并反馈', async () => {
  api.list.mockResolvedValue({ items: [{ id: 7, title: '测试工单', status: 'pending', category: 'bug', priority: 'low', description: '内容' }], total: 1 })
  api.confirm.mockRejectedValueOnce('cancel').mockResolvedValueOnce('confirm')
  api.close.mockRejectedValueOnce(new Error('关闭失败'))
  const wrapper = render(MaintenanceCenter); await flushPromises()
  const button = wrapper.findAll('button').find(b => b.text() === '关闭工单')!
  await button.trigger('click'); await flushPromises(); expect(api.close).not.toHaveBeenCalled()
  await button.trigger('click'); await flushPromises(); expect(api.close).toHaveBeenCalledWith(7)
  expect(wrapper.text()).toContain('关闭失败'); expect(wrapper.text()).toContain('待处理')
  wrapper.unmount()
})

for (const [component, action] of [[FeedbackCenter, '回复'], [MaintenanceCenter, '受理']] as const) {
  it(`管理员${action}失败在当前弹窗内显示且保留编辑内容`, async () => {
    api.admin = true
    api.list.mockResolvedValue({ items: [{ id: 8, title: '工单', content: '反馈', feedback_type: 'suggestion', status: 'pending', category: 'bug', priority: 'low' }], total: 1 })
    api.update.mockRejectedValueOnce(new Error('保存失败'))
    const wrapper = render(component); await flushPromises()
    await wrapper.findAll('button').find(b => b.text() === action)!.trigger('click')
    await wrapper.get('aside input').setValue('回复草稿')
    await wrapper.findAll('button').find(b => b.text() === '保存')!.trigger('click'); await flushPromises()
    expect(wrapper.get('aside').text()).toContain('保存失败')
    expect((wrapper.get('aside input').element as HTMLInputElement).value).toBe('回复草稿')
    expect(api.update).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
}
