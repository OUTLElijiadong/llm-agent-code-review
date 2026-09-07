import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const route = vi.hoisted(() => ({ params: { id: '42' }, query: {} as Record<string, string> }))
const router = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn(), back: vi.fn() }))
const reportApi = vi.hoisted(() => ({
  getReportDetail: vi.fn(),
  generateReport: vi.fn(),
  previewReport: vi.fn(),
  exportReport: vi.fn(),
}))
const reviewApi = vi.hoisted(() => ({ getTaskIssues: vi.fn() }))
const permissions = vi.hoisted(() => new Set<string>())
const messages = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }))

vi.mock('vue-router', () => ({ useRoute: () => route, useRouter: () => router }))
vi.mock('@/stores/user', () => ({ useUserStore: () => ({ hasPermission: (code: string) => permissions.has(code) }) }))
vi.mock('@/api/report', () => reportApi)
vi.mock('@/api/review', () => reviewApi)
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: messages }))

import ReportDetail from './ReportDetail.vue'

const issue = {
  id: 7,
  task_id: 42,
  file_name: 'src/auth.ts',
  line_number: 18,
  issue_type: 'security',
  severity: 'severe',
  title: '命令注入',
  description: '用户输入进入 shell',
  suggestion: '使用 argv 列表',
  remediation: '移除 shell=True',
  status: 'open',
  create_time: '2026-08-26T00:00:00Z',
  cvss_score: 9.8,
  cvss_vector: 'AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H',
  cvss_version: '3.1',
  cvss_source: 'vector',
}

function setupState(wrapper: VueWrapper): Record<string, any> {
  return (wrapper.vm as unknown as { $: { setupState: Record<string, any> } }).$.setupState
}

function mountPage(): VueWrapper {
  return shallowMount(ReportDetail, {
    global: {
      stubs: {
        'el-button': { template: '<button><slot /></button>' },
        'el-dialog': {
          name: 'ElDialog',
          props: ['modelValue'],
          emits: ['closed'],
          template: '<div v-if="modelValue" class="dialog-stub"><slot /></div>',
        },
        'el-dropdown': { template: '<div><slot /><slot name="dropdown" /></div>' },
        'el-dropdown-item': { template: '<div><slot /></div>' },
        'el-dropdown-menu': { template: '<div><slot /></div>' },
        'el-button-group': { template: '<div><slot /></div>' },
        'el-icon': { template: '<i><slot /></i>' },
        'el-option': true,
        'el-select': true,
        'el-statistic': true,
        BaseChart: true,
        EmptyState: true,
        PrismLoading: true,
      },
      directives: { loading: () => undefined },
    },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  route.query = {}
  permissions.clear()
  for (const code of ['report:view', 'issue:view', ...['json', 'html', 'pdf', 'word'].map(format => `report:export:${format}`)]) permissions.add(code)
  reportApi.getReportDetail.mockResolvedValue({
    project: { project_name: '测试项目', language: 'typescript' },
    task: { task_name: '安全审查', review_type: 'security', total_files: 1 },
    stats: { score: 0, total_issues: 1, severe: 1 },
    files: [],
    rules_snapshot: [],
  })
  reviewApi.getTaskIssues.mockResolvedValue({ items: [issue], total: 1 })
  reportApi.previewReport.mockResolvedValue('<!doctype html><html><body>report</body></html>')
  reportApi.exportReport.mockResolvedValue(new Blob(['report']))
  Object.defineProperty(window.URL, 'createObjectURL', {
    configurable: true,
    value: vi.fn(() => 'blob:report-preview'),
  })
  Object.defineProperty(window.URL, 'revokeObjectURL', {
    configurable: true,
    value: vi.fn(),
  })
})

describe('ReportDetail 报告口径', () => {
  it('沙箱4个未分级报告条目不展示四级全零和虚构修复进度', async () => {
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: { project_name: '沙箱项目' },
      task: { task_name: '沙箱报告', review_type: 'sandbox_test', total_files: 1 },
      stats: { score: 100, total_issues: 4, severe: 0, high: 0, medium: 0, low: 0, severity: { 未分级: 4 } },
      files: [], rules_snapshot: [],
      source: { type: 'sandbox_test', stats_basis: 'report_headings',
        report_issue_summary: { total: 4, unclassified: 4 } },
    })
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('条目数不代表已确认漏洞数')
    expect(wrapper.text()).toContain('报告条目')
    expect(wrapper.text()).toContain('未分级')
    expect(wrapper.find('.fix-gauge').exists()).toBe(false)
    expect(wrapper.get('[data-testid="report-risk-level"]').text()).toContain('安全风险未评定')
    expect(wrapper.get('[data-testid="report-risk-level"]').text()).not.toContain('低风险')
    expect(setupState(wrapper).severityRows).toHaveLength(1)
    wrapper.unmount()
  })

  it('领域报告只显示真实 JSON 出口并隐藏非等价导出和预览', async () => {
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: { project_name: '渗透项目', language: 'unknown' },
      task: { task_name: '渗透报告', review_type: 'pentest', total_files: 0 },
      stats: { score: 0, total_issues: 2 }, files: [], rules_snapshot: [],
      source: { type: 'pentest', stats_basis: 'pentest_findings' },
    })
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('[data-testid="report-export-word"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="report-export-pdf"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('生成 JSON')
    expect(wrapper.text()).not.toContain('生成 HTML')
    expect(wrapper.text()).not.toContain('生成 PDF')
    expect(wrapper.text()).not.toContain('生成 Word')
    expect(wrapper.text()).not.toContain('预览 HTML')
    wrapper.unmount()
  })

  it('导出请求期间重复调用只发出一次，并且错误不会保存文件', async () => {
    const pending = new Promise<Blob>(() => {})
    reportApi.exportReport.mockReturnValue(pending)
    const wrapper = mountPage()
    await flushPromises()
    const vm = setupState(wrapper)
    const first = vm.handleExport('json')
    const second = vm.handleExport('json')
    await flushPromises()
    expect(reportApi.exportReport).toHaveBeenCalledTimes(1)
    expect(vm.exportingFormat).toBe('json')
    expect(first).toBeInstanceOf(Promise)
    expect(second).toBeInstanceOf(Promise)
    wrapper.unmount()
  })

  it('导出错误展示后端message和next_action并提供重试操作', async () => {
    reportApi.exportReport.mockRejectedValueOnce({ code: 40941, message: '领域报告不支持 PDF', next_action: '请导出真实领域 JSON' })
    const wrapper = mountPage()
    await flushPromises()
    await setupState(wrapper).handleExport('pdf')
    expect(wrapper.text()).toContain('领域报告不支持 PDF')
    expect(wrapper.text()).toContain('请导出真实领域 JSON')
    expect(wrapper.findAll('button').some(button => button.text().includes('重试导出'))).toBe(true)
    wrapper.unmount()
  })

  it('兼容后端 task.name 字段并展示真实任务名', async () => {
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: { project_name: '测试项目', language: 'typescript' },
      task: { task_name: '  ', name: '后端真实任务名', review_type: 'security', total_files: 1 },
      stats: { score: 80, total_issues: 0 },
      files: [],
      rules_snapshot: [],
    })
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.find('.cover-task').text()).toContain('后端真实任务名')
    wrapper.unmount()
  })

  it('顶部 Word 和 PDF 按钮统一调用新导出接口并传递当前模板', async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.find('[data-testid="report-export-word"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="report-export-pdf"]').trigger('click')
    await flushPromises()

    expect(reportApi.exportReport).toHaveBeenNthCalledWith(1, 42, 'word', 'detailed')
    expect(reportApi.exportReport).toHaveBeenNthCalledWith(2, 42, 'pdf', 'detailed')
    expect(click).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('风险文案和颜色按最终分数计算，不受动画中间值影响', async () => {
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(() => 1)
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: { project_name: '测试项目', language: 'typescript' },
      task: { name: '风险口径验证', review_type: 'security', total_files: 1 },
      stats: { score: 60, total_issues: 0 },
      files: [],
      rules_snapshot: [],
    })
    const wrapper = mountPage()
    await flushPromises()

    const risk = wrapper.find('[data-testid="report-risk-level"]')
    expect(setupState(wrapper).animatedScore).toBe(0)
    expect(risk.text()).toBe('中风险')
    expect(risk.attributes('style')).toContain('rgb(217, 168, 87)')
    wrapper.unmount()
  })

  it('CVSS 缺失时显示未评分', async () => {
    const wrapper = mountPage()
    await flushPromises()

    expect(setupState(wrapper).cvssSeverityLabel(undefined)).toBe('未评分')
    wrapper.unmount()
  })

  it('只有 score 没有有效向量时不计入分布并显示未评分', async () => {
    reviewApi.getTaskIssues.mockResolvedValueOnce({
      items: [{
        ...issue,
        cvss_score: 7.5,
        cvss_vector: undefined,
        cvss_version: undefined,
        cvss_source: 'model',
      }],
      total: 1,
    })
    const wrapper = mountPage()
    await flushPromises()

    expect(setupState(wrapper).cvssTotal).toBe(0)
    expect(setupState(wrapper).top10Issues).toHaveLength(0)
    expect(
      wrapper.findAll('empty-state-stub')
        .some((node) => node.attributes('description') === 'CVSS 未评分'),
    ).toBe(true)
    expect(wrapper.text()).not.toContain('7.5')
    wrapper.unmount()
  })
})

describe('ReportDetail HTML 预览', () => {
  it('在网络等待前同步预开窗口，导航开始后才提示成功', async () => {
    let resolvePreview!: (html: string) => void
    reportApi.previewReport.mockReturnValueOnce(new Promise<string>((resolve) => {
      resolvePreview = resolve
    }))
    const replace = vi.fn()
    const popup = {
      opener: window,
      closed: false,
      location: { replace },
      document: { title: '', body: { textContent: '' } },
      close: vi.fn(),
    } as unknown as Window
    const open = vi.spyOn(window, 'open').mockReturnValue(popup)
    const wrapper = mountPage()
    await flushPromises()

    const pending = setupState(wrapper).handlePreview()

    expect(open).toHaveBeenCalledWith('about:blank', '_blank')
    expect(open.mock.invocationCallOrder[0]).toBeLessThan(
      reportApi.previewReport.mock.invocationCallOrder[
        reportApi.previewReport.mock.invocationCallOrder.length - 1
      ] as number,
    )
    expect(popup.opener).toBeNull()
    expect(messages.success).not.toHaveBeenCalled()

    resolvePreview('<!doctype html><html><body>report</body></html>')
    await pending

    expect(replace).toHaveBeenCalledWith('blob:report-preview')
    expect(messages.success).toHaveBeenCalledWith('HTML 报告已在新窗口打开')
    expect(messages.success).not.toHaveBeenCalledWith('HTML 报告已在当前页面打开')
    expect(replace.mock.invocationCallOrder[0]).toBeLessThan(
      messages.success.mock.invocationCallOrder[
        messages.success.mock.invocationCallOrder.length - 1
      ] as number,
    )
    wrapper.unmount()
  })

  it('弹窗被拦截时显示带 sandbox 的页内预览，不误报新窗口成功并在关闭后恢复焦点', async () => {
    vi.spyOn(window, 'open').mockReturnValue(null)
    const wrapper = mountPage()
    await flushPromises()
    const previewButton = wrapper.find('[data-testid="report-preview-button"]').element as HTMLElement
    const focus = vi.fn()
    Object.defineProperty(previewButton, 'focus', { configurable: true, value: focus })

    await setupState(wrapper).handlePreview()
    await flushPromises()

    const frame = wrapper.find('[data-testid="report-preview-fallback"]')
    expect(frame.exists()).toBe(true)
    expect(frame.attributes('sandbox')).toBe('')
    expect(frame.attributes('referrerpolicy')).toBe('no-referrer')
    expect(frame.attributes('srcdoc')).toContain('<!doctype html>')
    expect(messages.success).toHaveBeenCalledWith('HTML 报告已在当前页面打开')
    expect(messages.success).not.toHaveBeenCalledWith('HTML 报告已在新窗口打开')

    wrapper.findComponent({ name: 'ElDialog' }).vm.$emit('closed')
    await flushPromises()

    expect(focus).toHaveBeenCalledOnce()
    expect(wrapper.find('[data-testid="report-preview-fallback"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('无法切断 opener 时关闭预开窗口并回退到页内预览', async () => {
    const close = vi.fn()
    const popup = {
      closed: false,
      location: { replace: vi.fn() },
      document: { title: '', body: { textContent: '' } },
      close,
    }
    Object.defineProperty(popup, 'opener', {
      configurable: true,
      set: () => { throw new Error('opener is locked') },
    })
    vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
    const wrapper = mountPage()
    await flushPromises()

    await setupState(wrapper).handlePreview()
    await flushPromises()

    expect(close).toHaveBeenCalledOnce()
    expect(wrapper.find('[data-testid="report-preview-fallback"]').exists()).toBe(true)
    expect(messages.success).toHaveBeenCalledWith('HTML 报告已在当前页面打开')
    expect(messages.success).not.toHaveBeenCalledWith('HTML 报告已在新窗口打开')
    expect(popup.location.replace).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('预览接口失败时关闭预开窗口并且只显示错误', async () => {
    reportApi.previewReport.mockRejectedValueOnce(new Error('preview failed'))
    const popup = {
      opener: window,
      closed: false,
      location: { replace: vi.fn() },
      document: { title: '', body: { textContent: '' } },
      close: vi.fn(),
    } as unknown as Window
    vi.spyOn(window, 'open').mockReturnValue(popup)
    const wrapper = mountPage()
    await flushPromises()

    await setupState(wrapper).handlePreview()

    expect(popup.close).toHaveBeenCalledOnce()
    expect(messages.error).toHaveBeenCalledWith('预览报告失败')
    expect(messages.success).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})

describe('ReportDetail 详细修复方案', () => {
  it('点击后平滑滚动到真实详情区域并转移键盘焦点', async () => {
    vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: false } as MediaQueryList)
    const wrapper = mountPage()
    await flushPromises()
    const secondIssue = { ...issue, id: 8, title: '越权访问', cvss_score: 8.8 }
    setupState(wrapper).issues = [issue, secondIssue]
    await wrapper.vm.$nextTick()
    const section = wrapper.find('[data-testid="remediation-detail"]').element as HTMLElement
    const heading = wrapper.find('[data-testid="remediation-heading"]').element as HTMLElement
    const scrollIntoView = vi.fn()
    const focus = vi.fn()
    Object.defineProperty(section, 'scrollIntoView', { configurable: true, value: scrollIntoView })
    Object.defineProperty(heading, 'focus', { configurable: true, value: focus })
    const rows = wrapper.findAll('.top10-table tbody tr')
    expect(rows[0].attributes('aria-current')).toBe('true')
    expect(rows[1].attributes('aria-current')).toBeUndefined()

    await setupState(wrapper).selectRemediation(secondIssue.id)

    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'smooth', block: 'start' })
    expect(focus).toHaveBeenCalledWith({ preventScroll: true })
    expect(rows[0].attributes('aria-current')).toBeUndefined()
    expect(rows[1].attributes('aria-current')).toBe('true')
    wrapper.unmount()
  })

  it('用户开启减少动效时改为即时滚动', async () => {
    vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: true } as MediaQueryList)
    const wrapper = mountPage()
    await flushPromises()
    setupState(wrapper).issues = [issue]
    await wrapper.vm.$nextTick()
    const section = wrapper.find('[data-testid="remediation-detail"]').element as HTMLElement
    const scrollIntoView = vi.fn()
    Object.defineProperty(section, 'scrollIntoView', { configurable: true, value: scrollIntoView })
    Object.defineProperty(
      wrapper.find('[data-testid="remediation-heading"]').element,
      'focus',
      { configurable: true, value: vi.fn() },
    )

    await setupState(wrapper).selectRemediation(issue.id)

    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'auto', block: 'start' })
    wrapper.unmount()
  })
})


describe('ReportDetail 格式权限', () => {
  it('只读账号保留打印和预览，隐藏下载入口且直接调用也不发送导出请求', async () => {
    permissions.clear()
    permissions.add('report:view')
    route.query = { generate: '1' }
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('打印')
    expect(wrapper.find('[data-testid="report-preview-button"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('生成 JSON')
    expect(wrapper.text()).not.toContain('导出报告')
    expect(wrapper.find('[data-testid="report-export-word"]').exists()).toBe(false)
    const vm = setupState(wrapper)
    for (const format of ['json', 'html', 'pdf', 'word']) {
      await vm.handleGenerate(format)
      await vm.handleExport(format)
    }
    await vm.downloadWord()
    await vm.downloadPdf()
    expect(reportApi.generateReport).not.toHaveBeenCalled()
    expect(reportApi.exportReport).not.toHaveBeenCalled()
    expect(reviewApi.getTaskIssues).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('JSON 权限只开放 JSON 生成与导出', async () => {
    permissions.clear()
    permissions.add('report:view')
    permissions.add('report:export:json')
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('生成 JSON')
    expect(wrapper.text()).not.toContain('生成 HTML')
    expect(wrapper.find('[data-testid="report-export-pdf"]').exists()).toBe(false)
    await setupState(wrapper).handleExport('json')
    await setupState(wrapper).handleExport('pdf')
    expect(reportApi.exportReport).toHaveBeenCalledExactlyOnceWith(42, 'json', 'detailed')
    wrapper.unmount()
  })

  it('自动生成等待报告来源就绪，领域报告不会发送 HTML 请求', async () => {
    route.query = { generate: '1' }
    let resolveReport!: (value: unknown) => void
    reportApi.getReportDetail.mockReturnValueOnce(new Promise(resolve => { resolveReport = resolve }))
    const wrapper = mountPage()
    await flushPromises()
    expect(reportApi.generateReport).not.toHaveBeenCalled()
    resolveReport({ project: {}, task: {}, stats: {}, files: [], rules_snapshot: [], source: { type: 'sandbox_test' } })
    await flushPromises()
    expect(reportApi.generateReport).not.toHaveBeenCalled()
    expect(router.replace).toHaveBeenCalledWith({ query: {} })
    wrapper.unmount()
  })

  it('报告读取失败不自动生成，也不开放任何报告操作', async () => {
    route.query = { generate: '1' }
    reportApi.getReportDetail.mockRejectedValueOnce(new Error('not found'))
    const wrapper = mountPage()
    await flushPromises()
    expect(reportApi.generateReport).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="report-export-pdf"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('打印')
    wrapper.unmount()
  })
})
