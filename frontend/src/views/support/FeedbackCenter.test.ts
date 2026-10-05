import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { Feedback } from '@/api/feedback'

const state = vi.hoisted(() => ({
  profile: { id: 10, role: 'user' },
  list: vi.fn(), create: vi.fn(), update: vi.fn(), read: vi.fn(),
  message: { success: vi.fn(), warning: vi.fn() },
}))
vi.mock('@/api/feedback', () => ({ getFeedbackList: state.list, createFeedback: state.create,
  replyFeedback: state.update, markFeedbackRead: state.read }))
vi.mock('@/stores/user', async () => {
  const { reactive } = await import('vue')
  state.profile = reactive(state.profile)
  return { useUserStore: () => ({ profile: state.profile, isAdmin: () => state.profile.role === 'admin' }) }
})
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: state.message }))

import FeedbackCenter from './FeedbackCenter.vue'

const wrappers: VueWrapper[] = []
function record(overrides: Partial<Feedback> = {}): Feedback {
  return { id: 7, user_id: 10, feedback_type: 'bug', content: '反馈正文', status: 'new', admin_reply: null,
    create_time: '2026-10-06T01:00:00Z', update_time: '2026-10-06T01:00:00Z', ...overrides }
}
function page(items: Feedback[] = [record()]) { return { items, total: items.length, page: 1, page_size: 10, pages: 1 } }
function deferred<Value>() {
  let resolve!: (value: Value) => void
  const promise = new Promise<Value>(done => { resolve = done })
  return { promise, resolve }
}
function render(active = true) {
  const wrapper = mount(FeedbackCenter, { props: { active }, global: { directives: { loading: () => {} }, stubs: {
    ElCard: { template: '<section><slot/></section>' },
    ElButton: { props: ['loading', 'disabled'], template: '<button :disabled="loading || disabled"><slot/></button>' },
    ElAlert: { props: ['title'], template: '<section role="alert">{{ title }}<slot/></section>' },
    ElEmpty: { props: ['description'], template: '<p>{{ description }}</p>' },
    ElDialog: { props: ['modelValue'], template: '<aside v-if="modelValue"><slot/><slot name="footer"/></aside>' },
    ElForm: { template: '<form><slot/></form>' },
    ElFormItem: { props: ['label'], template: '<label>{{ label }}<slot/></label>' },
    ElInput: { props: ['modelValue', 'type'], template: '<textarea v-if="type === \'textarea\'" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)"/><input v-else :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)"/>' },
    ElSelect: { props: ['modelValue'], template: '<select :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value); $emit(\'change\', $event.target.value)"><slot/></select>' },
    ElOption: { props: ['label', 'value'], template: '<option :value="value">{{ label }}</option>' },
    ElRadioGroup: { props: ['modelValue'], template: '<select :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value); $emit(\'change\', $event.target.value)"><option value="mine">我的反馈</option><option value="all">全部反馈</option></select>' },
    ElRadioButton: true,
    ElDescriptions: { template: '<div><slot/></div>' }, ElDescriptionsItem: { template: '<div><slot/></div>' },
    ElPagination: { props: ['currentPage', 'total'], template: '<div><span>第{{ currentPage }}页 / 共{{ total }}条</span><button @click="$emit(\'current-change\', currentPage + 1)">下一页</button></div>' }, ElTag: { template: '<span><slot/></span>' },
  } } })
  wrappers.push(wrapper)
  return wrapper
}
function button(wrapper: VueWrapper, text: string) {
  return wrapper.findAll('button').find(item => item.text() === text)!
}
beforeEach(() => {
  vi.resetAllMocks()
  vi.useFakeTimers()
  state.profile.id = 10
  state.profile.role = 'user'
  state.list.mockResolvedValue(page())
  state.create.mockResolvedValue({ id: 8 })
  vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
})
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.useRealTimers() })

it('处理弹窗保留真实关闭状态，未修改不能保存，状态保存给出对应提示', async () => {
  state.profile.role = 'admin'
  state.list.mockResolvedValue(page([record({ status: 'closed', admin_reply: '原回复' })]))
  state.update.mockResolvedValue(record({ status: 'closed', admin_reply: '补充说明' }))
  const wrapper = render(); await flushPromises()
  await (button(wrapper, '回复 / 处理') ?? button(wrapper, '回复')).trigger('click')
  expect((wrapper.get('aside select').element as HTMLSelectElement).value).toBe('closed')
  expect(button(wrapper, '保存').attributes('disabled')).toBeDefined()
  await wrapper.get('aside textarea').setValue('补充说明')
  await button(wrapper, '保存').trigger('click'); await flushPromises()
  expect(state.update).toHaveBeenCalledWith(7, { admin_reply: '补充说明', status: 'closed' })
  expect(state.message.success).toHaveBeenCalledWith('回复已保存，反馈已关闭')
  expect(state.message.success).not.toHaveBeenCalledWith('已回复')
})

it.each(['new', 'read'])('原状态%s编辑非空回复时默认设为已回复，原状态初次打开保持不变', async status => {
  state.profile.role = 'admin'
  state.list.mockResolvedValue(page([record({ status })]))
  state.update.mockResolvedValue(record({ status: 'replied', admin_reply: '处理答复' }))
  const wrapper = render(); await flushPromises()
  await button(wrapper, '回复 / 处理').trigger('click')
  expect((wrapper.get('aside select').element as HTMLSelectElement).value).toBe(status)
  await wrapper.get('aside textarea').setValue('处理答复')
  expect((wrapper.get('aside select').element as HTMLSelectElement).value).toBe('replied')
  await button(wrapper, '保存').trigger('click'); await flushPromises()
  expect(state.update).toHaveBeenCalledWith(7, { admin_reply: '处理答复', status: 'replied' })
  expect(state.message.success).toHaveBeenCalledWith('反馈已回复')
})

it.each(['closed', 'new'])('管理员明确选择%s后，修改回复正文不会覆盖状态选择，提示同时说明回复和状态', async status => {
  state.profile.role = 'admin'
  state.update.mockResolvedValue(record({ status, admin_reply: '最终答复' }))
  const wrapper = render(); await flushPromises()
  await button(wrapper, '回复 / 处理').trigger('click')
  await wrapper.get('aside textarea').setValue('初稿')
  await wrapper.get('aside select').setValue(status)
  await wrapper.get('aside textarea').setValue('最终答复')
  expect((wrapper.get('aside select').element as HTMLSelectElement).value).toBe(status)
  await button(wrapper, '保存').trigger('click'); await flushPromises()
  expect(state.update).toHaveBeenCalledWith(7, { admin_reply: '最终答复', status })
  expect(state.message.success).toHaveBeenCalledWith('回复已保存，' + (status === 'closed' ? '反馈已关闭' : '反馈已设为待查看'))
})

it('空回复不能选择已回复后保存，直接触发处理函数也不会调用接口', async () => {
  state.profile.role = 'admin'
  const wrapper = render(); await flushPromises()
  await (button(wrapper, '回复 / 处理') ?? button(wrapper, '回复')).trigger('click')
  await wrapper.get('aside select').setValue('replied')
  await wrapper.get('aside textarea').setValue(' \n ')
  expect(wrapper.get('aside').text()).toContain('标记为已回复时，请填写回复内容')
  expect(button(wrapper, '保存').attributes('disabled')).toBeDefined()
  await (wrapper.vm as unknown as { submitReply(): Promise<void> }).submitReply()
  expect(state.update).not.toHaveBeenCalled()
})

it('显式标为已读调用既有POST并回读处理人时间，展开详情本身不写入', async () => {
  state.profile.role = 'admin'
  const read = record({ status: 'read', handled_by: 20, handled_at: '2026-10-06T02:00:00Z' })
  state.list.mockResolvedValueOnce(page()).mockResolvedValue(page([read]))
  state.read.mockResolvedValue(read)
  const wrapper = render(); await flushPromises()
  ;(wrapper.get('summary').element as HTMLElement).click()
  expect(state.read).not.toHaveBeenCalled()
  await button(wrapper, '标为已读').trigger('click'); await flushPromises()
  expect(state.read).toHaveBeenCalledWith(7)
  expect(wrapper.text()).toContain('已读')
  expect(wrapper.text()).toContain('管理员 #20')
  expect(wrapper.text()).toContain('处理时间')
  expect(button(wrapper, '标为已读')).toBeUndefined()
})

it.each([
  ['closed', '反馈已关闭'],
  ['replied', '反馈已回复'],
] as const)('并发处理后服务端返回%s时，提示采用最终状态', async (status, expectedMessage) => {
  state.profile.role = 'admin'
  const updated = record({ status, admin_reply: status === 'replied' ? '已处理' : null })
  state.list.mockResolvedValueOnce(page()).mockResolvedValue(page([updated]))
  state.read.mockResolvedValue(updated)
  const wrapper = render(); await flushPromises()
  await button(wrapper, '标为已读').trigger('click'); await flushPromises()
  expect(state.read).toHaveBeenCalledWith(7)
  expect(state.message.success).toHaveBeenCalledWith(expectedMessage)
  expect(wrapper.text()).toContain(status === 'closed' ? '已关闭' : '已回复')
})

it('管理员首次查看全部反馈，可按状态和类型过滤，并手动刷新当前筛选', async () => {
  state.profile.role = 'admin'
  const wrapper = render(); await flushPromises()
  expect(state.list).toHaveBeenLastCalledWith(expect.objectContaining({ scope: 'all', page: 1 }))
  await wrapper.get('[aria-label="反馈状态"]').setValue('closed'); await flushPromises()
  await wrapper.get('[aria-label="反馈类型"]').setValue('praise'); await flushPromises()
  await button(wrapper, '刷新反馈').trigger('click'); await flushPromises()
  expect(state.list).toHaveBeenLastCalledWith(expect.objectContaining({ status: 'closed', feedback_type: 'praise', page: 1 }))
})

it('非管理员只查看本人，无全部、回复或已读入口', async () => {
  const wrapper = render(); await flushPromises()
  expect(state.list).toHaveBeenLastCalledWith(expect.objectContaining({ scope: 'mine' }))
  expect(wrapper.find('[aria-label="反馈范围"]').exists()).toBe(false)
  expect(button(wrapper, '回复 / 处理')).toBeUndefined()
  expect(button(wrapper, '标为已读')).toBeUndefined()
})

it('仅激活的反馈页低频回读，管理员处理后本人看到回复更新提醒', async () => {
  const wrapper = render(); await flushPromises()
  state.list.mockResolvedValue(page([record({ status: 'replied', admin_reply: '管理员处理答复', handled_by: 20,
    handled_at: '2026-10-06T02:00:00Z' })]))
  await vi.advanceTimersByTimeAsync(30_000); await flushPromises()
  expect(state.list).toHaveBeenCalledTimes(2)
  expect(wrapper.text()).toContain('管理员处理答复')
  expect(wrapper.text()).toContain('反馈处理进度有更新')
  await wrapper.setProps({ active: false })
  await vi.advanceTimersByTimeAsync(90_000)
  expect(state.list).toHaveBeenCalledTimes(2)
})

it('反馈tab初始未激活不读取，重新进入才回读', async () => {
  const wrapper = render(false); await flushPromises()
  await vi.advanceTimersByTimeAsync(60_000)
  expect(state.list).not.toHaveBeenCalled()
  await wrapper.setProps({ active: true }); await flushPromises()
  expect(state.list).toHaveBeenCalledTimes(1)
})

it('隐藏页面停止回读，重新可见只读取一次', async () => {
  let hidden = false
  vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden)
  const wrapper = render(); await flushPromises()
  hidden = true; document.dispatchEvent(new Event('visibilitychange'))
  await vi.advanceTimersByTimeAsync(90_000)
  expect(state.list).toHaveBeenCalledTimes(1)
  hidden = false; document.dispatchEvent(new Event('visibilitychange')); await flushPromises()
  expect(state.list).toHaveBeenCalledTimes(2)
  wrapper.unmount()
})

it('自动回读不会重叠，打开提交弹窗暂停并保留草稿', async () => {
  const pending = deferred<ReturnType<typeof page>>()
  state.list.mockReturnValueOnce(pending.promise).mockResolvedValue(page())
  const wrapper = render(); await flushPromises()
  await vi.advanceTimersByTimeAsync(90_000)
  expect(state.list).toHaveBeenCalledTimes(1)
  pending.resolve(page()); await flushPromises()
  await button(wrapper, '提交反馈').trigger('click')
  await wrapper.get('aside textarea').setValue('未发送草稿')
  await vi.advanceTimersByTimeAsync(90_000)
  expect(state.list).toHaveBeenCalledTimes(1)
  expect((wrapper.get('aside textarea').element as HTMLTextAreaElement).value).toBe('未发送草稿')
})

it('授权失败清空旧记录并暂停自动回读，手动刷新成功恢复', async () => {
  const wrapper = render(); await flushPromises()
  state.list.mockRejectedValueOnce({ code: 40300, message: '权限失效' }).mockResolvedValue(page())
  await vi.advanceTimersByTimeAsync(30_000); await flushPromises()
  expect(wrapper.text()).not.toContain('反馈正文')
  expect(wrapper.text()).toContain('权限失效')
  await vi.advanceTimersByTimeAsync(90_000)
  expect(state.list).toHaveBeenCalledTimes(2)
  await button(wrapper, '刷新反馈').trigger('click'); await flushPromises()
  expect(wrapper.text()).toContain('反馈正文')
})

it('暂时读取失败保留上次快照并明确标记，错误不会伪装空态', async () => {
  const wrapper = render(); await flushPromises()
  state.list.mockRejectedValueOnce(new Error('暂时不可用'))
  await vi.advanceTimersByTimeAsync(30_000); await flushPromises()
  expect(wrapper.text()).toContain('反馈正文')
  expect(wrapper.text()).toContain('上次读取结果')
  expect(wrapper.text()).not.toContain('暂无反馈')
})

it('切换账号清除记录和编辑草稿，旧请求返回不得展示另一账号内容', async () => {
  const old = deferred<ReturnType<typeof page>>()
  state.list.mockReturnValueOnce(old.promise).mockResolvedValue(page([record({ id: 9, user_id: 11, content: '当前账号记录' })]))
  const wrapper = render(); await flushPromises()
  await button(wrapper, '提交反馈').trigger('click')
  await wrapper.get('aside textarea').setValue('旧账号草稿')
  state.profile.id = 11; await flushPromises()
  old.resolve(page([record({ content: '旧账号记录' })])); await flushPromises()
  expect(wrapper.text()).toContain('当前账号记录')
  expect(wrapper.text()).not.toContain('旧账号记录')
  expect(wrapper.find('aside').exists()).toBe(false)
})

it('卸载后迟到请求不回填，也不会留下轮询', async () => {
  const pending = deferred<ReturnType<typeof page>>()
  state.list.mockReturnValueOnce(pending.promise)
  const wrapper = render(); await flushPromises()
  wrapper.unmount()
  pending.resolve(page()); await flushPromises()
  await vi.advanceTimersByTimeAsync(90_000)
  expect(state.list).toHaveBeenCalledTimes(1)
  expect((wrapper.vm as unknown as { list: Feedback[] }).list).toEqual([])
})

it('翻页保留筛选，筛选变化返回第一页，刷新时越界页回到实际最后页', async () => {
  state.list.mockResolvedValue({ ...page(), total: 25, pages: 3 })
  const wrapper = render(); await flushPromises()
  await wrapper.get('[aria-label="反馈类型"]').setValue('bug'); await flushPromises()
  await button(wrapper, '下一页').trigger('click'); await flushPromises()
  expect(state.list).toHaveBeenLastCalledWith(expect.objectContaining({ feedback_type: 'bug', page: 2 }))
  await wrapper.get('[aria-label="反馈状态"]').setValue('read'); await flushPromises()
  expect(state.list).toHaveBeenLastCalledWith(expect.objectContaining({ feedback_type: 'bug', status: 'read', page: 1 }))
  await button(wrapper, '下一页').trigger('click'); await flushPromises()
  await button(wrapper, '下一页').trigger('click'); await flushPromises()
  state.list.mockResolvedValueOnce({ ...page([]), total: 11, pages: 2, page: 3 }).mockResolvedValue({ ...page(), total: 11, pages: 2, page: 2 })
  await button(wrapper, '刷新反馈').trigger('click'); await flushPromises()
  expect(state.list).toHaveBeenLastCalledWith(expect.objectContaining({ feedback_type: 'bug', status: 'read', page: 2 }))
  expect(wrapper.text()).toContain('第2页 / 共11条')
  expect(wrapper.text()).toContain('反馈正文')
})

it('自动回读更新已展开详情的回复，不折叠详情', async () => {
  const wrapper = render(); await flushPromises()
  const details = wrapper.get('details').element as HTMLDetailsElement
  ;(wrapper.get('summary').element as HTMLElement).click()
  expect(details.open).toBe(true)
  state.list.mockResolvedValue(page([record({ status: 'replied', admin_reply: '最新回复' })]))
  await vi.advanceTimersByTimeAsync(30_000); await flushPromises()
  expect(wrapper.get('details').element).toBe(details)
  expect(details.open).toBe(true)
  expect(wrapper.get('details').text()).toContain('最新回复')
})

it.each(['submit', 'reply', 'read'])('账号切换后%s动作的迟到成功不显示旧提示、不重载新账号', async action => {
  state.profile.role = action === 'submit' ? 'user' : 'admin'
  const pending = deferred<Feedback>()
  const method = action === 'submit' ? state.create : action === 'reply' ? state.update : state.read
  method.mockReturnValue(pending.promise)
  const wrapper = render(); await flushPromises()
  if (action === 'read') await button(wrapper, '标为已读').trigger('click')
  else {
    await button(wrapper, action === 'submit' ? '提交反馈' : '回复 / 处理').trigger('click')
    await wrapper.get('aside textarea').setValue('旧账号待提交内容')
    await button(wrapper, action === 'submit' ? '提交' : '保存').trigger('click')
  }
  await flushPromises()
  state.list.mockResolvedValue(page([record({ id: 9, user_id: 11, content: '新账号反馈' })]))
  state.profile.id = 11; await flushPromises()
  expect(wrapper.find('aside').exists()).toBe(false)
  pending.resolve(record({ status: 'replied', admin_reply: '旧账号回复' })); await flushPromises()
  expect(state.list).toHaveBeenCalledTimes(2)
  expect(wrapper.text()).toContain('新账号反馈')
  expect(wrapper.text()).not.toContain('旧账号回复')
  expect(state.message.success).not.toHaveBeenCalled()
})
