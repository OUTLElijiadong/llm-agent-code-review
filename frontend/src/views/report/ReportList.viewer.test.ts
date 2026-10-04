import { flushPromises, shallowMount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useUserStore } from '@/stores/user'
const api = vi.hoisted(() => ({ list: vi.fn(), projects: vi.fn(), remove: vi.fn(), export: vi.fn(), confirm: vi.fn(), push: vi.fn() }))
vi.mock('@/api/report', () => ({ getReports: api.list, deleteReport: api.remove, exportReport: api.export }))
vi.mock('@/api/project', () => ({ getProjects: api.projects }))
vi.mock('@/router', () => ({ default: { push: api.push } }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: api.push }) }))
vi.mock('@/composables/useDangerConfirm', () => ({ confirmDanger: api.confirm }))
import ReportList from './ReportList.vue'
const row = { task_id: 8, task_name: '本地只读报告样本', project_name: '本地项目', total_issues: 0, score: 100, status: 'success', create_time: '2026-10-04', can_delete: true }
let user: ReturnType<typeof useUserStore>
const wrappers: ReturnType<typeof shallowMount>[] = []
beforeEach(() => {
  vi.resetAllMocks()
  setActivePinia(createPinia())
  user = useUserStore()
  user.token = 'local-report-author'
  user.profile = { id: 11, username: 'local', role: 'user', status: 1 }
  user.permissions = new Set(['report:view', 'review:cancel'])
  api.list.mockResolvedValue({ items: [{ ...row }], total: 1 })
  api.projects.mockResolvedValue({ items: [] })
  api.confirm.mockResolvedValue(true)
})
afterEach(() => wrappers.splice(0).forEach(wrapper => wrapper.unmount()))
async function render() {
  const slot = { template: '<div><slot /><slot name="dropdown" /></div>' }
  const wrapper = shallowMount(ReportList, { global: { directives: { loading: () => {} }, stubs: {
    'el-card': slot, 'el-dropdown': slot, 'el-dropdown-menu': slot, 'el-dropdown-item': slot,
    'el-select': slot, 'el-option': true, 'el-date-picker': true, 'el-pagination': true,
  } } })
  wrappers.push(wrapper)
  await flushPromises()
  return { wrapper, vm: wrapper.vm as any }
}
describe('R3 报告删除精确能力（不借任务取消能力）', () => {
  it.each([false, undefined])('可读报告但删除能力拒绝/缺值时不能删除 %s', async canDelete => {
    const readonlyRow = { ...row, can_delete: canDelete }
    api.list.mockResolvedValue({ items: [readonlyRow], total: 1 })
    const { vm, wrapper } = await render()
    expect(wrapper.text()).not.toContain('删除报告')
    await vm.handleDelete(readonlyRow)
    expect(api.remove).not.toHaveBeenCalled(); expect(api.confirm).not.toHaveBeenCalled()
  })
  it.each(['账号', '全局权限', '资源能力'] as const)('确认期间%s变化不能提交旧报告删除', async scenario => {
    let resolve!: (value: boolean) => void
    api.confirm.mockReturnValueOnce(new Promise<boolean>(done => { resolve = done }))
    const { vm } = await render()
    const request = vm.handleDelete(vm.reports[0])
    if (scenario === '账号') user.token = 'another-session'
    else if (scenario === '全局权限') user.permissions.delete('review:cancel')
    else vm.reports[0].can_delete = false
    resolve(true); await request
    expect(api.remove).not.toHaveBeenCalled()
  })
  it('合法作者删除能力与全局权限仍可执行', async () => {
    const { vm } = await render()
    await vm.handleDelete(vm.reports[0])
    expect(api.remove).toHaveBeenCalledExactlyOnceWith(8)
  })
  it('R3 换账号后旧报告导出回执不能下载到新账号页面', async () => {
    user.permissions.add('report:export:json')
    let resolve!: (value: Blob) => void
    api.export.mockReturnValueOnce(new Promise<Blob>(done => { resolve = done }))
    const objectUrl = vi.fn(() => 'blob:local-report')
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: objectUrl })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const { vm } = await render()
    const request = vm.handleExport(vm.reports[0], 'json')
    user.token = 'another-session'
    resolve(new Blob(['old-account-report']))
    await request
    expect(objectUrl).not.toHaveBeenCalled()
    expect(click).not.toHaveBeenCalled()
    click.mockRestore()
  })
})
