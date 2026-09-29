import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'
const api = vi.hoisted(() => ({ list: vi.fn(), detail: vi.fn() }))
vi.mock('@/api/aiLog', () => ({ getAiLogs: api.list, getAiLogDetail: api.detail }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('@/router', () => ({ default: { push: vi.fn() } }))
import AiLogList from './AiLogList.vue'
import { useUserStore } from '@/stores/user'
const entry = { id: 1, model_name: 'fixture', status: 'success', create_time: '2026-09-20T12:50:00', total_tokens: 100 }
beforeEach(() => {
  setActivePinia(createPinia())
  api.list.mockReset().mockResolvedValue({ items: [entry], total: 1 })
  api.detail.mockReset()
})
function render() {
  return mount(AiLogList, { global: { directives: { loading: () => undefined }, stubs: {
    'el-card': { template: '<div><slot /></div>' }, 'el-select': true, 'el-option': true,
    'el-date-picker': true, 'el-tag': { template: '<span><slot /></span>' }, 'el-button': true,
    'el-icon': true, 'el-pagination': true,
  } } })
}
function deferred<Value>() {
  let resolve!: (value: Value) => void
  const promise = new Promise<Value>((done) => { resolve = done })
  return { promise, resolve }
}
it('被隔离原文明示原因，同时保留成本元数据', async () => {
  api.detail.mockResolvedValue({ ...entry, content_redacted: true, prompt: null, response: null, error_message: null })
  const wrapper = render()
  await flushPromises()
  await wrapper.get('[data-testid="log-card-1"]').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('原文按账号隔离')
  expect(wrapper.text()).toContain('100')
  wrapper.unmount()
})
it('切换账号后旧日志详情不能重新写入缓存', async () => {
  const user = useUserStore()
  user.profile = { id: 101, username: 'admin-a', role: 'admin', status: 1 }
  let resolve!: (value: unknown) => void
  api.detail.mockReturnValueOnce(new Promise(done => { resolve = done }))
  const wrapper = render()
  await flushPromises()
  await wrapper.get('[data-testid="log-card-1"]').trigger('click')
  user.profile = { id: 202, username: 'admin-b', role: 'admin', status: 1 }
  await flushPromises()
  resolve({ ...entry, prompt: '上一个管理员私密请求' })
  await flushPromises()
  expect(wrapper.text()).not.toContain('上一个管理员私密请求')
  wrapper.unmount()
})

it('日志列表请求失败显示重试错误，而不是误报空列表', async () => {
  api.list.mockRejectedValueOnce(new Error('temporary network failure'))
  const wrapper = render()
  await flushPromises()

  expect(wrapper.find('[data-testid="logs-error"]').exists()).toBe(true)
  expect(wrapper.text()).not.toContain('当前过滤条件下没有调用记录')

  api.list.mockResolvedValueOnce({ items: [], total: 0 })
  await wrapper.get('[data-testid="retry-logs"]').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('当前过滤条件下没有调用记录')
  wrapper.unmount()
})

it('日志列表加载中不短暂显示“没有调用记录”空态', async () => {
  const pending = deferred<{ items: typeof entry[]; total: number }>()
  api.list.mockReturnValueOnce(pending.promise)
  const wrapper = render()
  await flushPromises()

  expect(wrapper.text()).not.toContain('当前过滤条件下没有调用记录')
  pending.resolve({ items: [], total: 0 })
  await flushPromises()
  expect(wrapper.text()).toContain('当前过滤条件下没有调用记录')
  wrapper.unmount()
})
