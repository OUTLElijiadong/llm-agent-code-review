import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  reports: vi.fn(), projects: vi.fn(), export: vi.fn(), delete: vi.fn(), confirm: vi.fn(),
  permissions: new Set<string>(),
}))
const router = vi.hoisted(() => ({ push: vi.fn() }))
const messages = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() }))
vi.mock('@/api/report', () => ({ getReports: api.reports, exportReport: api.export, deleteReport: api.delete }))
vi.mock('@/api/project', () => ({ getProjects: api.projects }))
vi.mock('@/composables/useDangerConfirm', () => ({ confirmDanger: api.confirm }))
vi.mock('vue-router', () => ({ useRouter: () => router }))
vi.mock('@/stores/user', () => ({ useUserStore: () => ({
  hasPermission: (permission: string) => api.permissions.has(permission)
    || permission.startsWith('report:export:'),
}) }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: messages }))

import ReportList from './ReportList.vue'

const report = {
  task_id: 42, can_view_report: true, task_name: '渗透报告', project_name: '领域项目', total_issues: 3, score: 72,
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
        'el-tag': { template: '<span><slot /></span>' },
        EmptyState: { props: ['description'], template: '<div class="empty-state">{{ description }}</div>' },
      }, directives: { loading: {} },
    },
  })
}

beforeEach(() => {
  vi.resetAllMocks()
  api.permissions.clear()
  for (const permission of ['report:view', 'issue:view', 'security:view', 'pentest:view', 'review:start']) {
    api.permissions.add(permission)
  }
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
  it('缺少报告权限时不请求列表或展示卡片', async () => {
    api.permissions.delete('report:view')
    wrapper = mountPage()
    await flushPromises()

    expect(api.reports).not.toHaveBeenCalled()
    expect(api.projects).not.toHaveBeenCalled()
    expect(wrapper.find('.report-card').exists()).toBe(false)
    expect(wrapper.text()).toContain('当前账号没有报告查看权限')
  })

  it('服务端未明确授权的条目不渲染卡片或泄露分页总数', async () => {
    api.reports.mockResolvedValueOnce({
      items: [{ ...report, can_view_report: false, task_name: '其他账号的渗透报告' }],
      total: 38, page: 1, page_size: 20, pages: 2,
    })
    wrapper = mountPage()
    await flushPromises()

    expect(wrapper.find('.report-card').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('其他账号的渗透报告')
    expect(wrapper.text()).not.toContain('38')
  })

  it.each([
    { source: { type: 'pentest' }, permission: 'pentest:view' },
    { source: { type: 'security_scan', security_report: {} }, permission: 'security:view' },
  ])('来源权限 $permission 撤销后隐藏对应报告卡片', async ({ source, permission }) => {
    api.permissions.delete(permission)
    api.reports.mockResolvedValueOnce({
      items: [{ ...report, can_view_report: true, source }],
      total: 1, page: 1, page_size: 20, pages: 1,
    })
    wrapper = mountPage()
    await flushPromises()

    expect(wrapper.find('.report-card').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('渗透报告')
  })

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
    expect(text).toContain('已完成')
    expect(text).toContain('领域项目')
    expect(text).toContain('问题 3')
    expect(text).toContain('72')
    expect(text).toContain('2026-09-05')
  })

  it('局部验证保留记录分数但不伪装成完整通过或综合评分', async () => {
    api.reports.mockResolvedValueOnce({
      items: [{
        ...report,
        task_name: '白盒与黑盒基础检查',
        score: 100,
        source: { type: 'sandbox_test' },
        coverage: { verification_status: 'partial', reason: 'AI 动态用例未执行' },
      }],
      total: 1,
      page: 1,
      page_size: 20,
      pages: 1,
    })
    wrapper = mountPage()
    await flushPromises()

    const card = wrapper.find('.report-card')
    expect(card.text()).toContain('验证未完成')
    expect(card.text()).toContain('验证范围不完整')
    expect(card.text()).toContain('记录分数')
    expect(card.text()).toContain('100')
    expect(card.find('.rc-score').attributes('title')).toContain('不代表完整测试通过')
    expect(card.find('.rc-score').attributes('title')).not.toContain('综合评分')
  })

  it.each([
    { type: 'security_scan', coverage: { coverage_status: 'partial', reason: '部分文件未能扫描' } },
    { type: 'fullchain_audit', coverage: { verification_status: 'partial', reason: '语义分析阶段未完成' } },
    { type: 'pentest', coverage: { coverage_status: 'partial', reason: '部分验证阶段未执行' } },
  ])('$type 部分覆盖的成功任务显示覆盖不完整，而不是全部完成', async ({ type, coverage }) => {
    api.reports.mockResolvedValueOnce({
      items: [{
        ...report,
        task_name: '存在未覆盖范围的报告',
        source: { type, security_report: { coverage } },
        coverage,
      }],
      total: 1, page: 1, page_size: 20, pages: 1,
    })
    wrapper = mountPage()
    await flushPromises()

    const card = wrapper.find('.report-card')
    expect(card.text()).toContain('覆盖不完整')
    expect(card.text()).toContain(coverage.reason)
    expect(card.text()).not.toContain('已完成')
  })

  it('执行任务成功但覆盖验证失败时，不显示为成功报告', async () => {
    api.reports.mockResolvedValueOnce({
      items: [{
        ...report,
        task_name: '沙箱验证未通过',
        source: { type: 'sandbox_test' },
        coverage: { verification_status: 'failed', reason: '动态断言失败' },
      }],
      total: 1, page: 1, page_size: 20, pages: 1,
    })
    wrapper = mountPage()
    await flushPromises()

    const card = wrapper.find('.report-card')
    expect(card.text()).toContain('验证未通过')
    expect(card.text()).toContain('动态断言失败')
    expect(card.text()).not.toContain('已完成')
  })

  it('AI 报告未完成或历史状态未知时，在列表标出提示', async () => {
    api.reports.mockResolvedValueOnce({
      items: [{
        ...report,
        source: { type: 'sandbox_test', ai_review: { status: 'incomplete', failure_reason: '模型未配置' } },
      }],
      total: 1, page: 1, page_size: 20, pages: 1,
    })
    wrapper = mountPage()
    await flushPromises()
    const card = wrapper.find('.report-card')
    const warning = card.findAll('span').find(item => item.text().includes('AI 报告未完成或状态未知'))
    expect(warning).toBeTruthy()
    expect(warning?.attributes('title')).toBe('模型未配置')
  })

  it('长错误说明保持可访问全文，同时为列表提供限高样式', async () => {
    const error = `扫描覆盖不完整：${'测试代理返回无效内容；'.repeat(60)}`
    api.reports.mockResolvedValueOnce({
      items: [{ ...report, status: 'failed', error_message: error }],
      total: 1, page: 1, page_size: 20, pages: 1,
    })
    wrapper = mountPage()
    await flushPromises()

    const note = wrapper.get('.rc-status-note')
    expect(note.attributes('title')).toBe(`执行说明：${error}`)
    expect(note.attributes('aria-label')).toBe(`执行说明：${error}`)
  })

  it('远程目标 4xx 的局部验证标为未完成，不误报成确认测试失败', async () => {
    api.reports.mockResolvedValueOnce({
      items: [{
        ...report,
        task_name: '受保护路由黑盒核验',
        status: 'failed',
        source: { type: 'sandbox_test' },
        coverage: { verification_status: 'partial', reason: '远程目标返回 HTTP 401' },
      }],
      total: 1,
      page: 1,
      page_size: 20,
      pages: 1,
    })
    wrapper = mountPage()
    await flushPromises()

    const card = wrapper.find('.report-card')
    expect(card.text()).toContain('验证未完成')
    expect(card.text()).not.toContain('未通过')
  })

  it('确定性执行失败仍显示未通过', async () => {
    api.reports.mockResolvedValueOnce({
      items: [{
        ...report,
        task_name: '黑盒断言失败',
        status: 'failed',
        source: { type: 'sandbox_test' },
        coverage: { verification_status: 'failed', reason: '动态断言失败' },
      }],
      total: 1,
      page: 1,
      page_size: 20,
      pages: 1,
    })
    wrapper = mountPage()
    await flushPromises()

    const card = wrapper.find('.report-card')
    expect(card.text()).toContain('未通过')
    expect(card.text()).not.toContain('验证未完成')
  })

  it.each([
    { status: 'failed', label: '执行失败' },
    { status: 'cancelled', label: '已取消' },
    { status: 'expired', label: '授权已过期' },
    { status: 'stopped', label: '已停止' },
  ])('$status 状态明确显示执行终态和失败原因', async ({ status, label }) => {
    api.reports.mockResolvedValueOnce({
      items: [{ ...report, status, error_message: '报告未覆盖：远程响应超时' }],
      total: 1, page: 1, page_size: 20, pages: 1,
    })
    wrapper = mountPage()
    await flushPromises()

    const card = wrapper.find('.report-card')
    expect(card.text()).toContain(label)
    expect(card.text()).toContain('报告未覆盖：远程响应超时')
    expect(card.text()).not.toContain('通过')
  })

  it('执行结果缺失时显示结果未知，不推断成断言失败', async () => {
    api.reports.mockResolvedValueOnce({
      items: [{
        ...report,
        task_name: 'Worker 回执缺失',
        status: 'failed',
        source: { type: 'sandbox_test' },
        coverage: { verification_status: 'unknown', reason: '没有完整 Worker 结果' },
      }],
      total: 1,
      page: 1,
      page_size: 20,
      pages: 1,
    })
    wrapper = mountPage()
    await flushPromises()

    const card = wrapper.find('.report-card')
    expect(card.text()).toContain('结果未知')
    expect(card.text()).not.toContain('未通过')
  })

  it('请求未完成时不把空数组渲染成“暂无报告”或 0 条', async () => {
    let resolveReports!: (value: { items: never[]; total: number }) => void
    api.reports.mockReturnValueOnce(new Promise(resolve => { resolveReports = resolve }))
    wrapper = mountPage()
    await flushPromises()

    expect(wrapper.find('.empty-state').exists()).toBe(false)
    expect(wrapper.find('.pagination-wrapper').exists()).toBe(false)

    resolveReports({ items: [], total: 0 })
    await flushPromises()
    expect(wrapper.find('.empty-state').text()).toBe('暂无审查报告')
    expect(wrapper.find('.pagination-wrapper').exists()).toBe(true)
  })

  it('列表请求失败时显示失败状态，不伪装成空报告列表', async () => {
    api.reports.mockRejectedValueOnce(new Error('network unavailable'))
    wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('报告列表加载失败，请重试。')
    expect(wrapper.find('.empty-state').exists()).toBe(false)
    expect(wrapper.find('.pagination-wrapper').exists()).toBe(false)
  })

  it('点击失败态重试后恢复报告列表', async () => {
    api.reports.mockRejectedValueOnce(new Error('network unavailable'))
    wrapper = mountPage()
    await flushPromises()

    await wrapper.findAll('button').find(button => button.text() === '重试加载')?.trigger('click')
    await flushPromises()

    expect(api.reports).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.report-card').text()).toContain('渗透报告')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('较早筛选请求晚返回时不能覆盖新结果', async () => {
    let resolveOlder!: (value: { items: typeof report[]; total: number }) => void
    let resolveNewer!: (value: { items: typeof report[]; total: number }) => void
    api.reports
      .mockReturnValueOnce(new Promise(resolve => { resolveOlder = resolve }))
      .mockReturnValueOnce(new Promise(resolve => { resolveNewer = resolve }))
    wrapper = mountPage()
    await flushPromises()
    const vm = wrapper.vm as any
    const newerRequest = vm.loadData()

    resolveNewer({ items: [{ ...report, task_name: '新筛选结果' }], total: 1 })
    await newerRequest
    resolveOlder({ items: [{ ...report, task_name: '旧筛选结果' }], total: 1 })
    await flushPromises()

    expect(wrapper.find('.report-card').text()).toContain('新筛选结果')
    expect(wrapper.find('.report-card').text()).not.toContain('旧筛选结果')
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

  it('导出文件名清理内部 review_type 后缀', async () => {
    let filename = ''
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      filename = this.download
    })
    wrapper = mountPage()
    await flushPromises()
    await (wrapper.vm as any).handleExport({
      ...report,
      task_name: '项目167 完整代码审查（review_type=full）',
    }, 'json')

    expect(filename).toBe('review_report_项目167 完整代码审查_42.json')
    expect(filename).not.toContain('review_type=')
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
