import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const route = vi.hoisted(() => ({ params: { id: '42' }, query: {} as Record<string, string> }))
const router = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn(), back: vi.fn() }))
const reportApi = vi.hoisted(() => ({
  getReportDetail: vi.fn(),
  exportWord: vi.fn(),
  exportPdf: vi.fn(),
  generateReport: vi.fn(),
  previewReport: vi.fn(),
  exportReport: vi.fn(),
}))
const reviewApi = vi.hoisted(() => ({ getTaskIssues: vi.fn() }))
const messages = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }))

vi.mock('vue-router', () => ({ useRoute: () => route, useRouter: () => router }))
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
  route.query = {}
  reportApi.getReportDetail.mockResolvedValue({
    project: { project_name: '测试项目', language: 'typescript' },
    task: { task_name: '安全审查', review_type: 'security', total_files: 1 },
    stats: { score: 0, total_issues: 1, severe: 1 },
    files: [],
    rules_snapshot: [],
  })
  reviewApi.getTaskIssues.mockResolvedValue({ items: [issue], total: 1 })
  reportApi.previewReport.mockResolvedValue('<!doctype html><html><body>report</body></html>')
  Object.defineProperty(window.URL, 'createObjectURL', {
    configurable: true,
    value: vi.fn(() => 'blob:report-preview'),
  })
  Object.defineProperty(window.URL, 'revokeObjectURL', {
    configurable: true,
    value: vi.fn(),
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
