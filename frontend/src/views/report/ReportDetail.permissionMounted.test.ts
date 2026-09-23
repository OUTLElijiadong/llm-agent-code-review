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

it('报告封面区分项目主语言与本次审查文件语言', async () => {
  api.getReportDetail.mockResolvedValueOnce({
    ...report,
    project: { project_name: '手工作坊管理系统', language: 'javascript' },
    files: [{ file_name: 'SecurityConfig.java', language: 'java', issue_count: 0, severe_count: 0, score: 100 }],
  })
  await render()
  const tags = wrapper.findAll('.cover-tag').map(tag => tag.text())
  expect(tags).toContain('项目语言：javascript')
  expect(tags).toContain('审查语言：java')
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
