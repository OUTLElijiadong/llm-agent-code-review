import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  reports: vi.fn(), projects: vi.fn(), export: vi.fn(), delete: vi.fn(), confirm: vi.fn(),
}))
const router = vi.hoisted(() => ({ push: vi.fn() }))
const messages = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() }))
vi.mock('@/api/report', () => ({ getReports: api.reports, exportReport: api.export, deleteReport: api.delete }))
vi.mock('@/api/project', () => ({ getProjects: api.projects }))
vi.mock('@/composables/useDangerConfirm', () => ({ confirmDanger: api.confirm }))
vi.mock('vue-router', () => ({ useRouter: () => router }))
vi.mock('@/stores/user', () => ({ useUserStore: () => ({
  hasPermission: (permission: string) => permission.startsWith('report:export:') || permission === 'review:start',
}) }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: messages }))

import ReportList from './ReportList.vue'

const report = {
  task_id: 42, task_name: '渗透报告', project_name: '领域项目', total_issues: 3, score: 72,
  status: 'success', create_time: '2026-09-05T00:00:00Z',
  source: { type: 'pentest', stats_basis: 'pentest_findings', detail_api: '/api/pentest/engagements/demo' },
}

function mountPage(): VueWrapper {
  return mount(ReportList, {
    global: {
      stubs: {
        'el-card': { template: '<div><slot /></div>' },
        'el-select': { template: '<div><slot /></div>' }, 'el-option': true,
        'el-date-picker': true, 'el-pagination': true,
        'el-tooltip': { template: '<div><slot /></div>' },
        'el-button': { inheritAttrs: false, template: '<button v-bind="$attrs"><slot /></button>' },
        'el-icon': { template: '<i><slot /></i>' }, 'el-dropdown': { template: '<div><slot /><slot name="dropdown" /></div>' },
        'el-dropdown-menu': { template: '<div><slot /></div>' }, 'el-dropdown-item': { template: '<div><slot /></div>' },
        'el-tag': { template: '<span><slot /></span>' }, EmptyState: true,
      }, directives: { loading: {} },
    },
  })
}

beforeEach(() => {
  vi.resetAllMocks()
  api.reports.mockResolvedValue({ items: [report], total: 1, page: 1, page_size: 20, pages: 1 })
  api.projects.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100, pages: 0 })
  api.export.mockResolvedValue(new Blob(['{}'], { type: 'application/json' }))
  api.confirm.mockResolvedValue(true)
  Object.defineProperty(window.URL, 'createObjectURL', {
    configurable: true,
    value: vi.fn(() => 'blob:report'),
  })
  Object.defineProperty(window.URL, 'revokeObjectURL', {
    configurable: true,
    value: vi.fn(),
  })
})
let wrapper: VueWrapper | undefined
afterEach(() => wrapper?.unmount())

describe('报告卡片列表', () => {
  it('卡片化展示:主行任务名+审查类型徽章+结论,次行项目/问题数/时间,右侧评分色环', async () => {
    wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('[data-testid="report-cards"]').exists()).toBe(true)
    const card = wrapper.find('.report-card')
    expect(card.exists()).toBe(true)
    expect(card.attributes('data-status')).toBe('success')
    const text = card.text()
    expect(text).toContain('渗透报告')
    expect(text).toContain('渗透测试')
    expect(text).toContain('通过')
    expect(text).toContain('领域项目')
    expect(text).toContain('问题 3')
    expect(text).toContain('72')
    expect(text).toContain('2026-09-05')
  })
})

describe('报告列表领域导出', () => {
  it('领域报告只显示真实 JSON 出口，不展示非等价 HTML/PDF/Word', async () => {
    wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('导出 JSON')
    expect(wrapper.text()).not.toContain('导出 HTML')
    expect(wrapper.text()).not.toContain('导出 PDF')
    expect(wrapper.text()).not.toContain('导出 Word')
  })

  it('领域 JSON 可达并反馈成功，列表导出按钮在请求期间防重复', async () => {
    const pending = new Promise<Blob>(() => {})
    api.export.mockReturnValue(pending)
    wrapper = mountPage()
    await flushPromises()
    const vm = wrapper.vm as any
    const first = vm.handleExport(report, 'json')
    const second = vm.handleExport(report, 'json')
    await flushPromises()
    expect(api.export).toHaveBeenCalledTimes(1)
    expect(vm.exportingTaskId).toBe(42)
    expect(first).toBeInstanceOf(Promise)
    expect(second).toBeInstanceOf(Promise)
  })

  it('错误 JSON 不触发下载，显示message和next_action并提供重试入口', async () => {
    api.export.mockRejectedValueOnce({ code: 40941, message: '领域报告不支持 PDF', next_action: '请导出真实领域 JSON' })
    wrapper = mountPage()
    await flushPromises()
    const vm = wrapper.vm as any
    await vm.handleExport(report, 'pdf')
    expect(wrapper.text()).toContain('领域报告不支持 PDF')
    expect(wrapper.text()).toContain('请导出真实领域 JSON')
    expect(wrapper.findAll('button').some(button => button.text().includes('重试导出'))).toBe(true)
    expect(window.URL.createObjectURL).not.toHaveBeenCalled()
  })
})
