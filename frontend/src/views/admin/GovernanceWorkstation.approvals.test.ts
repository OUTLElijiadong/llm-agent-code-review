import { flushPromises, shallowMount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ listApprovals: vi.fn(), listJobs: vi.fn() }))
vi.mock('@/api/adminGovernance', () => api)
vi.mock('@/stores/user', async () => {
  const { reactive } = await import('vue')
  const user = reactive({
    profile: { id: 101, username: 'admin-a', role: 'super_admin', status: 1 },
    token: 'approval-token-a',
    isSuperAdmin: () => true,
  })
  return { useUserStore: () => user }
})
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { error: vi.fn() } }))
vi.mock('element-plus/es/components/message-box/index', () => ({ ElMessageBox: { confirm: vi.fn() } }))

import GovernanceWorkstation from './GovernanceWorkstation.vue'
import { useUserStore } from '@/stores/user'

const ButtonStub = {
  inheritAttrs: false,
  props: ['disabled', 'loading'],
  emits: ['click'],
  template: '<button v-bind="$attrs" :disabled="disabled" @click="$emit(\'click\')"><slot /></button>',
}
const TableStub = {
  props: ['data'],
  template: '<div class="approval-table"><slot v-if="!data?.length" name="empty" /></div>',
}
const EmptyStateStub = { props: ['description'], template: '<div class="empty-state">{{ description }}<slot /></div>' }

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

function render() {
  return shallowMount(GovernanceWorkstation, {
    props: { mode: 'approvals' },
    global: {
      stubs: {
        'el-button': ButtonStub,
        'el-table': TableStub,
        'el-table-column': true,
        EmptyState: EmptyStateStub,
      },
      directives: { loading: {} },
    },
  })
}

beforeEach(() => {
  const user = useUserStore()
  user.profile = { id: 101, username: 'admin-a', role: 'super_admin', status: 1 }
  user.token = 'approval-token-a'
  api.listApprovals.mockReset()
  api.listJobs.mockReset()
})

it('审批接口失败显示错误而不是空待办，原位重试后恢复成功空态', async () => {
  api.listApprovals.mockRejectedValueOnce(new Error('审批服务暂不可用'))
  const wrapper = render()
  await flushPromises()

  expect(wrapper.get('[role="alert"]').text()).toContain('审批服务暂不可用')
  expect(wrapper.find('.empty-state').exists()).toBe(false)

  api.listApprovals.mockResolvedValueOnce([])
  await wrapper.get('[role="alert"] button').trigger('click')
  await flushPromises()
  expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  expect(wrapper.get('.empty-state').text()).toContain('无待审批事项')
  expect(api.listApprovals).toHaveBeenCalledTimes(2)
  wrapper.unmount()
})

it('审批列表滚动区可聚焦、可命名，并提示小屏横向浏览', async () => {
  api.listApprovals.mockResolvedValueOnce([])
  const wrapper = render()
  await flushPromises()
  const region = wrapper.get('[data-testid="approval-table-scroll"]')
  expect(region.attributes('role')).toBe('region')
  expect(region.attributes('aria-label')).toContain('待办审批')
  expect(region.attributes('tabindex')).toBe('0')
  expect(wrapper.text()).toContain('左右滑动可查看状态和操作列')
  wrapper.unmount()
})

it('待审批请求尚未完成时不显示“无待审批事项”空态', async () => {
  const pending = deferred<unknown[]>()
  api.listApprovals.mockReturnValueOnce(pending.promise)
  const wrapper = render()
  await wrapper.vm.$nextTick()

  expect(wrapper.find('.empty-state').exists()).toBe(false)
  expect(wrapper.get('[role="status"]').text()).toContain('正在加载待审批事项')
  pending.resolve([])
  await flushPromises()
  expect(wrapper.get('.empty-state').text()).toContain('无待审批事项')
  wrapper.unmount()
})

it('审批刷新请求乱序完成时旧失败不能清除最新结果或提前结束加载态', async () => {
  const oldRequest = deferred<unknown[]>()
  const newRequest = deferred<unknown[]>()
  const newerItem = { id: 92, title: '较新的审批', agent_code: 'reviewer', action: 'agent_package.publish', risk_level: 'high', status: 'pending' }
  api.listApprovals.mockReturnValueOnce(oldRequest.promise).mockReturnValueOnce(newRequest.promise)
  const wrapper = render()
  const vm = setupState<{
    loadData: () => Promise<void>
    loading: boolean
    approvals: Array<{ id: number }>
    approvalLoadError: string
  }>(wrapper)

  await wrapper.vm.$nextTick()
  expect(wrapper.find('.page-head button').attributes('disabled')).toBeDefined()
  const newerLoad = vm.loadData()
  newRequest.resolve([newerItem])
  await flushPromises()
  expect(vm.approvals.map(item => item.id)).toEqual([92])

  oldRequest.reject(new Error('旧审批请求失败'))
  await Promise.all([newerLoad, flushPromises()])
  expect(vm.approvals.map(item => item.id)).toEqual([92])
  expect(vm.approvalLoadError).toBe('')
  expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  wrapper.unmount()
})

it.each(['token', 'profile'] as const)('切换 %s 后旧审批响应不写回新账号状态', async (identityPart) => {
  const oldRequest = deferred<unknown[]>()
  const latestRequest = deferred<unknown[]>()
  const latestItem = { id: 103, title: '新账号审批', agent_code: 'reviewer', action: 'agent_package.publish', risk_level: 'high', status: 'pending' }
  api.listApprovals.mockReturnValueOnce(oldRequest.promise).mockReturnValueOnce(latestRequest.promise)
  const wrapper = render()
  const user = useUserStore()
  const vm = setupState<{
    approvals: Array<{ id: number }>
    loading: boolean
  }>(wrapper)

  await wrapper.vm.$nextTick()
  expect(api.listApprovals).toHaveBeenCalledTimes(1)
  if (identityPart === 'token') user.token = 'approval-token-b'
  else user.profile = { id: 202, username: 'admin-b', role: 'super_admin', status: 1 }
  oldRequest.resolve([{ id: 101, title: '旧账号审批' }])
  await flushPromises()
  expect(vm.approvals).toEqual([])
  expect(vm.loading).toBe(true)
  expect(api.listApprovals).toHaveBeenCalledTimes(2)

  latestRequest.resolve([latestItem])
  await flushPromises()
  expect(vm.approvals.map(item => item.id)).toEqual([103])
  expect(wrapper.text()).not.toContain('旧账号审批')
  expect(vm.loading).toBe(false)
  wrapper.unmount()
})

it('切换管理页模式后旧审批响应不写入隐藏状态，也不提前结束新页面加载', async () => {
  const oldApproval = deferred<unknown[]>()
  const jobsRequest = deferred<unknown[]>()
  api.listApprovals.mockReturnValueOnce(oldApproval.promise)
  api.listJobs.mockReturnValueOnce(jobsRequest.promise)
  const wrapper = render()
  const vm = setupState<{
    approvals: Array<{ id: number }>
    jobs: Array<{ id: number }>
    loading: boolean
  }>(wrapper)

  await wrapper.vm.$nextTick()
  await wrapper.setProps({ mode: 'jobs' })
  await wrapper.vm.$nextTick()
  expect(api.listJobs).toHaveBeenCalledTimes(1)
  expect(vm.loading).toBe(true)

  oldApproval.resolve([{ id: 777, title: '旧模式审批' }])
  await flushPromises()
  expect(vm.approvals).toEqual([])
  expect(vm.loading).toBe(true)

  jobsRequest.resolve([])
  await flushPromises()
  expect(vm.jobs).toEqual([])
  expect(vm.loading).toBe(false)
  wrapper.unmount()
})
