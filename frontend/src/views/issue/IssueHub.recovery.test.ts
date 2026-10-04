import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'
import IssueHub from './IssueHub.vue'

const mocks = vi.hoisted(() => ({ projects: vi.fn(), issues: vi.fn(), push: vi.fn(), update: vi.fn(), batch: vi.fn(), confirm: vi.fn() }))
vi.mock('@/api/project', () => ({ getProjects: mocks.projects }))
vi.mock('@/api/issue', () => ({ list: mocks.issues, updateStatus: mocks.update, batchUpdateStatus: mocks.batch }))
vi.mock('@/stores/user', () => ({ useUserStore: () => user }))
vi.mock('element-plus/es/components/message-box/index', () => ({ ElMessageBox: { confirm: mocks.confirm } }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: mocks.push }) }))

const row = { id: 1, title: '问题一', task_id: 10, severity: '中', status: 'unfixed', issue_type: '安全漏洞', can_handle: true }
const user = reactive({ profile: { id: 11 }, token: 'account-a', permissions: [] as string[], hasPermission: (code: string) => user.permissions.includes(code) })
const Slot = { template: '<div><slot /></div>' }
function render() {
  return mount(IssueHub, { global: {
    directives: { loading: () => {} },
    stubs: {
      ElCard: Slot, ElSelect: Slot,
      ElOption: { props: ['label', 'value'], template: '<div :data-value="value">{{ label }}</div>' },
      ElInput: Slot, ElTag: Slot, ElIcon: Slot, ElPagination: Slot,
      ElDropdown: Slot, ElDropdownMenu: Slot, ElDropdownItem: Slot,
      ElAlert: { props: ['title'], template: '<section role="alert"><b>{{ title }}</b><slot /></section>' },
      ElButton: { props: ['loading', 'disabled'], template: '<button :disabled="loading || disabled"><slot /></button>' },
      EmptyState: { props: ['description'], template: '<div>{{ description }}</div>' },
    },
  } })
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

beforeEach(() => {
  mocks.projects.mockReset().mockResolvedValue({ items: [] })
  mocks.issues.mockReset().mockResolvedValue({ items: [row], total: 1 })
  mocks.batch.mockReset().mockResolvedValue({})
  mocks.update.mockReset().mockResolvedValue({})
  mocks.confirm.mockReset().mockResolvedValue(true)
  user.profile = { id: 11 }
  user.token = 'account-a'
  user.permissions = ['issue:view', 'project:view']
})

describe('问题追踪失败恢复', () => {
  it.each([false, undefined])('R3 只读或未确认处理能力的问题不显示操作且直接调用无效 %s', async (canHandle) => {
    user.permissions.push('issue:handle', 'issue:batch')
    const readonlyRow = { ...row, can_handle: canHandle }
    mocks.issues.mockResolvedValueOnce({ items: [readonlyRow], total: 1 })
    const wrapper = render()
    await flushPromises()
    const vm = wrapper.vm as any
    expect(wrapper.find('.ic-check').exists()).toBe(false)
    vm.toggleSelect(readonlyRow)
    await vm.onSetStatus(readonlyRow, 'fixed')
    vm.selected = [readonlyRow]
    await vm.onBatchMarkFixed()
    expect(mocks.update).not.toHaveBeenCalled()
    expect(mocks.batch).not.toHaveBeenCalled()
    expect(mocks.confirm).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('多个关联任务显示名净化已知内部后缀且不改写原任务名', async () => {
    const issueRows = [
      { ...row, task_id: 180, task_name: '项目167 完整代码审查（review_type=full）' },
      { ...row, id: 2, task_id: 179, task_name: '项目167 完整代码审查 (review_type=full)' },
      { ...row, id: 3, task_id: 178, task_name: '业务审查（review_type=experimental）' },
    ]
    mocks.issues.mockResolvedValueOnce({ items: issueRows, total: issueRows.length })
    const wrapper = render()
    await flushPromises()
    const metadata = wrapper.findAll('.ic-line2')
    expect(metadata[0].text()).toContain('项目167 完整代码审查')
    expect(metadata[0].text()).not.toContain('review_type=')
    expect(metadata[1].text()).not.toContain('review_type=')
    expect(metadata[2].text()).toContain('业务审查（review_type=experimental）')
    expect(issueRows[0].task_name).toBe('项目167 完整代码审查（review_type=full）')
    wrapper.unmount()
  })

  it('筛选器明确显示后端默认的处理中范围，并保留全部状态入口', async () => {
    const wrapper = render()
    await flushPromises()

    expect((wrapper.vm as any).filters.status).toBe('active')
    expect(wrapper.text()).toContain('处理中（未修复/待复查）')
    expect(wrapper.text()).toContain('全部状态')
    expect(mocks.issues).toHaveBeenCalledWith(expect.objectContaining({ status: undefined }))
    wrapper.unmount()
  })

  it.each(['2026-09-20T12:51:10', '2026-09-20T12:51:10Z', '2026-09-20T20:51:10+08:00'])('服务端时间 %s 在非 UTC 浏览器显示本地时间', async (createdAt) => {
    vi.stubEnv('TZ', 'Asia/Shanghai')
    const wrapper = render()
    try {
      expect(new Date().getTimezoneOffset()).toBe(-480)
      mocks.issues.mockResolvedValue({ items: [{ ...row, create_time: createdAt }], total: 1 })
      await flushPromises()
      await (wrapper.vm as any).loadIssues()
      expect(wrapper.get('.ic-line2').text()).toContain('2026-09-20 20:51')
    } finally {
      wrapper.unmount()
      vi.unstubAllEnvs()
    }
  })

  it('项目筛选失败不阻断问题加载，重试项目只刷新项目选项', async () => {
    mocks.projects.mockRejectedValueOnce(new Error('项目读取失败'))
    const wrapper = render()
    await flushPromises()
    expect(mocks.issues).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('问题一')
    const error = wrapper.get('[data-testid="issue-project-load-error"]')
    expect(error.text()).toContain('项目读取失败')
    await error.get('button').trigger('click')
    await flushPromises()
    expect(mocks.projects).toHaveBeenCalledTimes(2)
    expect(mocks.issues).toHaveBeenCalledOnce()
    expect(wrapper.find('[data-testid="issue-project-load-error"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('问题请求失败明确提示且不显示空列表，点击重试恢复卡片', async () => {
    mocks.issues.mockRejectedValueOnce(new Error('问题服务暂不可用'))
    const wrapper = render()
    await flushPromises()
    expect(wrapper.text()).not.toContain('暂无问题')
    const error = wrapper.get('[data-testid="issue-load-error"]')
    expect(error.text()).toContain('问题服务暂不可用')
    await error.get('button').trigger('click')
    await flushPromises()
    expect(mocks.issues).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-testid="issue-load-error"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('问题一')
    wrapper.unmount()
  })

  it('刷新失败保留已有结果并标明旧结果，成功空响应才显示暂无问题', async () => {
    const wrapper = render()
    await flushPromises()
    mocks.issues.mockRejectedValueOnce({ message: '筛选请求失败' })
    await (wrapper.vm as any).loadIssues()
    expect(wrapper.text()).toContain('问题一')
    expect(wrapper.get('[data-testid="issue-load-error"]').text()).toContain('上次成功加载的结果')
    mocks.issues.mockResolvedValueOnce({ items: [], total: 0 })
    await wrapper.get('[data-testid="issue-load-error"] button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('暂无问题')
    expect(wrapper.find('.issue-card').exists()).toBe(false)
    wrapper.unmount()
  })

  it('迟到的旧项目问题响应不能覆盖当前项目筛选', async () => {
    const wrapper = render()
    await flushPromises()
    const vm = wrapper.vm as any
    const old = deferred<any>()
    const current = deferred<any>()
    mocks.issues.mockImplementation((query: { project_id?: number }) => (
      query.project_id === 1 ? old.promise : current.promise
    ))
    vm.filters.project_id = 1
    const staleRequest = vm.loadIssues()
    vm.filters.project_id = 2
    const currentRequest = vm.loadIssues()
    current.resolve({ items: [{ ...row, id: 2, title: '项目B问题' }], total: 1 })
    await currentRequest
    old.resolve({ items: [{ ...row, title: '项目A旧问题' }], total: 1 })
    await staleRequest

    expect(vm.filters.project_id).toBe(2)
    expect(wrapper.text()).toContain('项目B问题')
    expect(wrapper.text()).not.toContain('项目A旧问题')
    wrapper.unmount()
  })

  it('读取权限撤销后清空旧问题、总数和勾选', async () => {
    const wrapper = render()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.selected = [row]
    mocks.issues.mockRejectedValueOnce({ code: 40301, message: '没有查看权限' })

    await vm.loadIssues()

    expect(wrapper.text()).not.toContain('问题一')
    expect(vm.total).toBe(0)
    expect(vm.selected).toEqual([])
    expect(wrapper.get('[data-testid="issue-load-error"]').text()).toContain('旧问题已清空')
    wrapper.unmount()
  })

  it('批量确认期间勾选变化，只提交确认时冻结的问题ID', async () => {
    user.permissions = ['issue:view', 'project:view', 'issue:batch']
    const wrapper = render()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.selected = [row]
    const confirmation = deferred<boolean>()
    mocks.confirm.mockReturnValueOnce(confirmation.promise)
    const request = vm.onBatchMarkFixed()
    vm.selected = [{ ...row, id: 2, title: '未确认的问题' }]
    confirmation.resolve(true)
    await request
    expect(mocks.batch).toHaveBeenCalledExactlyOnceWith({ ids: [1], status: 'fixed' })
    wrapper.unmount()
  })

  it.each(['account', 'permission', 'filter'])('批量确认期间%s变化后取消旧操作', async (change) => {
    user.permissions = ['issue:view', 'project:view', 'issue:batch']
    const wrapper = render()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.selected = [row]
    const confirmation = deferred<boolean>()
    mocks.confirm.mockReturnValueOnce(confirmation.promise)
    const request = vm.onBatchMarkFixed()
    if (change === 'account') user.profile = { id: 12 }
    if (change === 'permission') user.permissions = []
    if (change === 'filter') vm.filters.project_id = 999
    await flushPromises()
    confirmation.resolve(true)
    await request
    expect(mocks.batch).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('账号切换清空已有问题并忽略旧账号迟到的问题和项目选项', async () => {
    const wrapper = render()
    await flushPromises()
    const vm = wrapper.vm as any
    const oldIssues = deferred<any>()
    const oldProjects = deferred<any>()
    mocks.issues.mockReturnValueOnce(oldIssues.promise).mockResolvedValue({ items: [], total: 0 })
    mocks.projects.mockReturnValueOnce(oldProjects.promise).mockResolvedValue({ items: [] })
    const issueRequest = vm.loadIssues()
    const projectRequest = vm.loadProjects()
    user.profile = { id: 12 }
    user.token = 'account-b'
    await flushPromises()
    expect(wrapper.text()).not.toContain('问题一')
    oldIssues.resolve({ items: [{ ...row, title: '旧账号迟到问题' }], total: 1 })
    oldProjects.resolve({ items: [{ id: 7, project_name: '旧账号项目' }] })
    await Promise.all([issueRequest, projectRequest])
    expect(wrapper.text()).not.toContain('旧账号迟到问题')
    expect(vm.projects).toEqual([])
    wrapper.unmount()
  })

  it.each(['permission', 'filter'])('批量确认期间%s离开再恢复也不复用旧确认', async (change) => {
    user.permissions = ['issue:view', 'project:view', 'issue:batch']
    const wrapper = render()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.selected = [row]
    const confirmation = deferred<boolean>()
    mocks.confirm.mockReturnValueOnce(confirmation.promise)
    const request = vm.onBatchMarkFixed()
    if (change === 'permission') {
      user.permissions = ['issue:view', 'project:view']
      user.permissions = ['issue:view', 'project:view', 'issue:batch']
    } else {
      vm.filters.project_id = 999
      vm.filters.project_id = undefined
    }
    confirmation.resolve(true)
    await request
    expect(mocks.batch).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('待确认操作不能被重复点击生成第二次批量写请求', async () => {
    user.permissions = ['issue:view', 'project:view', 'issue:batch']
    const wrapper = render()
    await flushPromises()
    const vm = wrapper.vm as any
    vm.selected = [row]
    const confirmation = deferred<boolean>()
    mocks.confirm.mockReturnValueOnce(confirmation.promise)
    const request = vm.onBatchMarkFixed()
    await vm.onBatchMarkFixed()
    expect(mocks.confirm).toHaveBeenCalledTimes(1)
    confirmation.resolve(true)
    await request
    expect(mocks.batch).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
})
