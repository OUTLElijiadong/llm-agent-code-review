import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'
const api = vi.hoisted(() => ({ list: vi.fn() }))
vi.mock('@/api/audit', () => ({ listAuditLogs: api.list }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('@/router', () => ({ default: { push: vi.fn() } }))
import SystemAudit from './SystemAudit.vue'
import { useUserStore } from '@/stores/user'
const entry = { id: 1, actor_id: 101, actor_name: '账号 A', action: 'agent_team.create', target_type: 'agent_team', target_id: '1', status: 'success', create_time: '2026-09-20T12:50:00' }
beforeEach(() => { setActivePinia(createPinia()); api.list.mockReset() })
function render() {
  return mount(SystemAudit, { global: { directives: { loading: () => undefined }, stubs: {
    'el-card': { template: '<div><slot /></div>' }, 'el-select': true, 'el-option': true, 'el-input': true,
    'el-date-picker': true, 'el-tag': { template: '<span><slot /></span>' },
    'el-button': { inheritAttrs: false, emits: ['click'], template: '<button v-bind="$attrs" @click="$emit(\'click\')"><slot /></button>' },
    'el-icon': true, 'el-pagination': true,
    EmptyState: { props: ['description'], template: '<div class="empty-state">{{ description }}<slot /></div>' },
  } } })
}
function setupState<T extends Record<string, unknown>>(wrapper: ReturnType<typeof render>): T {
  return (wrapper.vm as unknown as { $: { setupState: T } }).$.setupState
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}
it('私人操作原文明确隔离且不会通过 title 属性旁路暴露', async () => {
  api.list.mockResolvedValue({ items: [{ ...entry, content_redacted: true, detail: '不应出现的旧字段' }], total: 1 })
  const wrapper = render(); await flushPromises()
  await wrapper.get('.audit-card').trigger('click')
  expect(wrapper.text()).toContain('原文按账号隔离')
  expect(wrapper.html()).not.toContain('不应出现的旧字段')
  expect(wrapper.text()).toContain('账号 A')
  wrapper.unmount()
})
it('旧账号未完成的审计列表不能回写到新账号', async () => {
  const user = useUserStore(); user.profile = { id: 101, username: 'a', role: 'admin', status: 1 }
  let resolve!: (value: unknown) => void
  api.list.mockReturnValueOnce(new Promise(done => { resolve = done }))
  const wrapper = render()
  api.list.mockResolvedValue({ items: [], total: 0 })
  user.profile = { id: 202, username: 'b', role: 'admin', status: 1 }; await flushPromises()
  resolve({ items: [{ ...entry, detail: '账号 A 的私密审计内容' }], total: 1 }); await flushPromises()
  expect(wrapper.find('.audit-card').exists()).toBe(false)
  wrapper.unmount()
})

it('加载失败显示错误与重试，重试成功空结果后才显示空态', async () => {
  api.list.mockRejectedValueOnce(new Error('审计服务暂不可用'))
  const wrapper = render(); await flushPromises()
  expect(wrapper.get('[role="alert"]').text()).toContain('审计服务暂不可用')
  expect(wrapper.find('.empty-state').exists()).toBe(false)

  api.list.mockResolvedValueOnce({ items: [], total: 0 })
  await wrapper.get('[role="alert"] button').trigger('click'); await flushPromises()
  expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  expect(wrapper.get('.empty-state').text()).toContain('暂无审计记录')
  expect(api.list).toHaveBeenCalledTimes(2)
  wrapper.unmount()
})

it('同账号筛选请求乱序完成时只接受最新结果且旧请求不提前结束加载态', async () => {
  const oldRequest = deferred<{ items: typeof entry[]; total: number }>()
  const newRequest = deferred<{ items: typeof entry[]; total: number }>()
  api.list.mockReturnValueOnce(oldRequest.promise).mockReturnValueOnce(newRequest.promise)
  const wrapper = render()
  const vm = setupState<{
    filters: { keyword: string }
    loadLogs: () => Promise<void>
    loading: boolean
    page: number
    rows: typeof entry[]
  }>(wrapper)

  vm.filters.keyword = '较新的关键词'
  vm.page = 3
  const latestLoad = vm.loadLogs()
  oldRequest.resolve({ items: [{ ...entry, id: 11 }], total: 1 })
  await flushPromises()

  expect(vm.loading).toBe(true)
  expect(vm.rows).toEqual([])
  expect(api.list).toHaveBeenNthCalledWith(2, expect.objectContaining({ keyword: '较新的关键词', page: 3 }))

  newRequest.resolve({ items: [{ ...entry, id: 12 }], total: 1 })
  await Promise.all([latestLoad, flushPromises()])
  expect(vm.rows.map(row => row.id)).toEqual([12])
  expect(vm.loading).toBe(false)
  wrapper.unmount()
})

it('较早的筛选请求后到失败不会清除最新成功结果或显示旧错误', async () => {
  const oldRequest = deferred<{ items: typeof entry[]; total: number }>()
  api.list.mockReturnValueOnce(oldRequest.promise).mockResolvedValueOnce({
    items: [{ ...entry, id: 22, actor_name: '新筛选结果' }], total: 1,
  })
  const wrapper = render()
  const vm = setupState<{
    filters: { keyword: string }
    loadLogs: () => Promise<void>
    rows: typeof entry[]
    loadError: string
  }>(wrapper)

  vm.filters.keyword = '最新筛选'
  await vm.loadLogs()
  expect(vm.rows.map(row => row.id)).toEqual([22])

  oldRequest.reject(new Error('旧筛选超时'))
  await flushPromises()
  expect(vm.rows.map(row => row.id)).toEqual([22])
  expect(vm.loadError).toBe('')
  expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  wrapper.unmount()
})

it('详情内按钮的 Enter 和 Space 不冒泡为审计卡片折叠操作', async () => {
  api.list.mockResolvedValue({ items: [{ ...entry, target_type: 'project', target_id: '15' }], total: 1 })
  const wrapper = render(); await flushPromises()
  const card = wrapper.get('.audit-card')
  await card.trigger('click')
  expect(card.attributes('aria-expanded')).toBe('true')

  const detailButton = wrapper.get('.ac-detail button')
  await detailButton.trigger('keydown.enter')
  expect(wrapper.get('.audit-card').attributes('aria-expanded')).toBe('true')
  await detailButton.trigger('keydown.space')
  expect(wrapper.get('.audit-card').attributes('aria-expanded')).toBe('true')
  wrapper.unmount()
})
