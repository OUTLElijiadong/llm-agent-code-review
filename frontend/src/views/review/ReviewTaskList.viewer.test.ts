import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useUserStore } from '@/stores/user'
import { deferred, pageOf, scanMountOptions } from './scanRegressionTestUtils'
const api = vi.hoisted(() => ({ tasks: vi.fn(), projects: vi.fn(), cancel: vi.fn(), remove: vi.fn(), confirm: vi.fn(), push: vi.fn() }))
vi.mock('@/api/review', () => ({ getReviewTasks: api.tasks, cancelReviewTask: api.cancel, deleteReviewTask: api.remove }))
vi.mock('@/api/project', () => ({ getProjects: api.projects }))
vi.mock('@/router', () => ({ default: { push: api.push } }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: api.push }) }))
vi.mock('@/composables/useDangerConfirm', () => ({ confirmDanger: api.confirm }))
import ReviewTaskList from './ReviewTaskList.vue'
let user: ReturnType<typeof useUserStore>
const row = { id: 1, task_name: '本地资源门样本', status: 'running', can_cancel: true }
const wrappers: ReturnType<typeof mount>[] = []
beforeEach(() => {
  vi.resetAllMocks()
  setActivePinia(createPinia())
  user = useUserStore()
  user.token = 'local-review-owner'
  user.profile = { id: 11, username: 'local', role: 'user', status: 1 }
  user.permissions = new Set(['review:view', 'review:cancel'])
  api.tasks.mockResolvedValue(pageOf([{ ...row }]))
  api.projects.mockResolvedValue(pageOf([]))
  api.confirm.mockResolvedValue(true)
})
afterEach(() => wrappers.splice(0).forEach(wrapper => wrapper.unmount()))
async function render() {
  const wrapper = mount(ReviewTaskList, scanMountOptions)
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, vm: wrapper.vm as any }
}
describe('R3 任务取消能力（实际 store，本地 API 桩）', () => {
  it.each([false, undefined])('任务只读或能力缺值时按钮与直接单/批处理都拒绝 %s', async canCancel => {
    const readonlyRow = { ...row, can_cancel: canCancel }
    api.tasks.mockResolvedValue(pageOf([readonlyRow]))
    const { vm, wrapper } = await render()
    expect(wrapper.get('.tc-actions').text()).not.toContain('停止')
    expect(wrapper.get('.tc-actions').text()).not.toContain('删除')
    await vm.handleCancel(readonlyRow); await vm.handleDelete(readonlyRow)
    vm.selectedRows = [readonlyRow]
    await vm.handleBatchStop(); await vm.handleBatchDelete()
    expect(api.cancel).not.toHaveBeenCalled(); expect(api.remove).not.toHaveBeenCalled()
    expect(api.confirm).not.toHaveBeenCalled()
  })
  it.each(['账号', '全局权限', '资源能力'] as const)('确认等待中%s变化不提交旧任务操作', async scenario => {
    const { vm } = await render()
    const confirmation = deferred<boolean>()
    api.confirm.mockReturnValueOnce(confirmation.promise)
    const request = vm.handleCancel(vm.tasks[0])
    if (scenario === '账号') user.token = 'another-session'
    else if (scenario === '全局权限') user.permissions.delete('review:cancel')
    else vm.tasks[0].can_cancel = false
    confirmation.resolve(true)
    await request
    expect(api.cancel).not.toHaveBeenCalled()
  })
  it('合法项目写能力和全局取消权限仍可停止', async () => {
    const { vm } = await render()
    await vm.handleCancel(vm.tasks[0])
    expect(api.cancel).toHaveBeenCalledExactlyOnceWith(1)
  })
})
