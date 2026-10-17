import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useUserStore } from '@/stores/user'

const api = vi.hoisted(() => ({ getReportDetail: vi.fn(), generateReport: vi.fn(), previewReport: vi.fn(), exportReport: vi.fn() }))
const issues = vi.hoisted(() => ({ getTaskIssues: vi.fn() }))
vi.mock('@/api/report', () => api)
vi.mock('@/api/review', () => issues)
vi.mock('vue-router', async original => ({ ...await original<typeof import('vue-router')>(),
  useRoute: () => ({ params: { id: '42' }, query: {} }), useRouter: () => ({ replace: vi.fn(), push: vi.fn(), back: vi.fn() }) }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { error: vi.fn(), success: vi.fn() } }))
import ReportDetail from './ReportDetail.vue'

let wrapper: VueWrapper
let pinia: ReturnType<typeof createPinia>
const report = { project: { project_name: '隔离验收报告' }, task: { name: '报告', total_files: 1 },
  stats: { score: 100, total_issues: 0 }, files: [], rules_snapshot: [], source: { type: 'full' } }

beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  useUserStore().profile = { id: 103, username: 'qa_report', role: 'user', status: 1 }
  useUserStore().permissions = new Set(['report:view'])
  api.getReportDetail.mockResolvedValue(report)
  api.generateReport.mockResolvedValue('{}')
  api.exportReport.mockResolvedValue(new Blob(['{}'], { type: 'application/json' }))
  api.previewReport.mockResolvedValue('<!doctype html><html><body>隔离预览</body></html>')
  issues.getTaskIssues.mockResolvedValue({ items: [], total: 0 })
  vi.spyOn(window, 'open').mockReturnValue(null)
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
  Object.defineProperty(window.URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:isolated-report') })
  Object.defineProperty(window.URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
})
afterEach(() => { wrapper?.unmount(); document.body.innerHTML = '' })

async function render() {
  wrapper = mount(ReportDetail, { attachTo: document.body, global: { plugins: [ElementPlus, pinia],
    stubs: { BaseChart: true, PrismLoading: true, EmptyState: true } } })
  await flushPromises()
}
function button(label: string) { return wrapper.findAll('button').find(node => node.text().includes(label)) }
function state() { return (wrapper.vm as unknown as { $: { setupState: Record<string, (...args: string[]) => Promise<void>> } }).$.setupState }
async function exportJsonFromMenu() {
  await button('导出报告')!.trigger('click')
  await flushPromises()
  await vi.waitFor(() => expect(document.body.querySelector('.el-dropdown-menu__item')).not.toBeNull())
  const items = [...document.body.querySelectorAll<HTMLElement>('.el-dropdown-menu__item')]
  expect(items.map(node => node.textContent?.trim())).toEqual(['JSON'])
  items[0].click()
  await flushPromises()
}

it('仅查看权限实际点击HTML预览仍可用，不触发下载或无权问题请求', async () => {
  await render()
  expect(button('打印')).toBeDefined()
  expect(button('导出报告')).toBeUndefined()
  await wrapper.get('[data-testid="report-preview-button"]').trigger('click')
  await flushPromises()
  expect(api.previewReport).toHaveBeenCalledExactlyOnceWith(42, 'detailed')
  expect(document.querySelector('[data-testid="report-preview-fallback"]')?.getAttribute('sandbox')).toBe('')
  expect(api.generateReport).not.toHaveBeenCalled()
  expect(api.exportReport).not.toHaveBeenCalled()
  expect(issues.getTaskIssues).not.toHaveBeenCalled()
})

it('报告查看权限撤销后立即清除已加载正文，并明确标注问题详情不可见', async () => {
  api.getReportDetail.mockResolvedValueOnce({
    ...report,
    summary: '不可继续显示的报告内容',
    stats: { score: 100, total_issues: 2 },
  })
  await render()
  expect(wrapper.text()).toContain('不可继续显示的报告内容')
  expect(wrapper.get('[data-testid="report-issues-access-limited"]').text())
    .toContain('已隐藏逐条问题与修复建议')

  useUserStore().permissions.delete('report:view')
  await flushPromises()

  expect(wrapper.find('[data-testid="report-paper"]').exists()).toBe(false)
  expect(wrapper.find('[data-testid="report-access-revoked"]').exists()).toBe(true)
  expect(wrapper.text()).not.toContain('不可继续显示的报告内容')
})

it('问题明细读取失败时明确提示不完整并支持重试', async () => {
  useUserStore().permissions.add('issue:view')
  api.getReportDetail.mockResolvedValueOnce({
    ...report,
    stats: { score: 100, total_issues: 2 },
  })
  issues.getTaskIssues
    .mockRejectedValueOnce(new Error('temporary issue service error'))
    .mockResolvedValueOnce({ items: [], total: 2 })

  await render()

  expect(wrapper.get('[data-testid="report-issues-load-error"]').text())
    .toContain('逐条问题、严重度分布和修复建议尚未完整加载')
  expect(wrapper.find('[data-testid="report-issues-access-limited"]').exists()).toBe(false)

  await button('重试读取问题明细')!.trigger('click')
  await flushPromises()

  expect(wrapper.find('[data-testid="report-issues-load-error"]').exists()).toBe(false)
  expect(issues.getTaskIssues).toHaveBeenCalledTimes(2)
})

it('问题详情权限撤销后立即清除已读取的问题数据', async () => {
  useUserStore().permissions.add('issue:view')
  api.getReportDetail.mockResolvedValueOnce({
    ...report,
    stats: { score: 100, total_issues: 1 },
  })
  issues.getTaskIssues.mockResolvedValueOnce({ items: [{ id: 9, title: '权限撤销后不可见' }], total: 1 })

  await render()
  expect((state().issues as unknown as Array<{ title: string }>)[0]?.title).toBe('权限撤销后不可见')

  useUserStore().permissions.delete('issue:view')
  await flushPromises()

  expect(state().issues).toEqual([])
  expect(wrapper.find('[data-testid="report-issues-load-error"]').exists()).toBe(false)
  expect(wrapper.get('[data-testid="report-issues-access-limited"]').text())
    .toContain('已隐藏逐条问题与修复建议')
})

it('JSON独立权限通过真实生成按钮与ElementPlus下拉菜单完成对应调用', async () => {
  useUserStore().permissions.add('report:export:json')
  await render()
  expect(button('生成 HTML')).toBeUndefined()
  expect(button('导出 PDF')).toBeUndefined()
  await button('生成 JSON')!.trigger('click')
  await flushPromises()
  expect(api.generateReport).toHaveBeenCalledExactlyOnceWith(42, 'json', 'detailed')
  await exportJsonFromMenu()
  expect(api.exportReport).toHaveBeenCalledExactlyOnceWith(42, 'json', 'detailed')
})

it('真实Pinia撤回格式权限后按钮隐藏，原错误重试handler也不能再下载', async () => {
  useUserStore().permissions.add('report:export:json')
  api.exportReport.mockRejectedValueOnce({ message: '隔离错误', next_action: '请重试' })
  await render()
  await exportJsonFromMenu()
  expect(button('重试导出')).toBeDefined()
  useUserStore().permissions.delete('report:export:json')
  await flushPromises()
  expect(button('导出报告')).toBeUndefined()
  expect(button('生成 JSON')).toBeUndefined()
  expect(button('重试导出')).toBeUndefined()
  await state().retryExport()
  await state().handleGenerate('json')
  expect(api.exportReport).toHaveBeenCalledOnce()
  expect(api.generateReport).not.toHaveBeenCalled()
})

it.each(['sandbox_test', 'pentest'])('来源未加载时无操作；%s已加载后只开放JSON交集', async (type) => {
  for (const format of ['json', 'html', 'pdf', 'word']) useUserStore().permissions.add(`report:export:${format}`)
  let resolve!: (value: typeof report) => void
  api.getReportDetail.mockReturnValueOnce(new Promise(value => { resolve = value }))
  await render()
  expect(button('打印')).toBeUndefined()
  expect(button('导出 PDF')).toBeUndefined()
  await state().downloadPdf()
  await state().handleGenerate('html')
  expect(api.exportReport).not.toHaveBeenCalled()
  expect(api.generateReport).not.toHaveBeenCalled()
  resolve({ ...report, source: { type } })
  await flushPromises()
  expect(button('生成 JSON')).toBeDefined()
  expect(button('生成 HTML')).toBeUndefined()
  expect(wrapper.find('[data-testid="report-preview-button"]').exists()).toBe(false)
  await state().handleExport('html')
  await state().downloadWord()
  expect(api.exportReport).not.toHaveBeenCalled()
  if (type === 'sandbox_test') expect(wrapper.get('[data-testid="report-risk-level"]').text()).toContain('安全风险未评定')
})
