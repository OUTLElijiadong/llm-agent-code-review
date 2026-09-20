import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import IssueHub from './IssueHub.vue'

const mocks = vi.hoisted(() => ({ projects: vi.fn(), issues: vi.fn(), push: vi.fn() }))
vi.mock('@/api/project', () => ({ getProjects: mocks.projects }))
vi.mock('@/api/issue', () => ({ list: mocks.issues, updateStatus: vi.fn(), batchUpdateStatus: vi.fn() }))
vi.mock('@/stores/user', () => ({ useUserStore: () => ({ hasPermission: () => false }) }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: mocks.push }) }))

const row = { id: 1, title: '问题一', task_id: 10, severity: '中', status: 'unfixed', issue_type: '安全漏洞' }
const Slot = { template: '<div><slot /></div>' }
function render() {
  return mount(IssueHub, { global: {
    directives: { loading: () => {} },
    stubs: {
      ElCard: Slot, ElSelect: Slot, ElOption: Slot, ElInput: Slot, ElTag: Slot, ElIcon: Slot, ElPagination: Slot,
      ElAlert: { props: ['title'], template: '<section role="alert"><b>{{ title }}</b><slot /></section>' },
      ElButton: { props: ['loading', 'disabled'], template: '<button :disabled="loading || disabled"><slot /></button>' },
      EmptyState: { props: ['description'], template: '<div>{{ description }}</div>' },
    },
  } })
}

beforeEach(() => {
  mocks.projects.mockReset().mockResolvedValue({ items: [] })
  mocks.issues.mockReset().mockResolvedValue({ items: [row], total: 1 })
})

describe('问题追踪失败恢复', () => {
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
})
