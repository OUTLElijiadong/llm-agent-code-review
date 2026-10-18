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
  stats: { score: 100, total_issues: 0 }, summary: '账号专属报告摘要', files: [], rules_snapshot: [], source: { type: 'full' } }

beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  useUserStore().profile = { id: 103, username: 'qa_report', role: 'user', status: 1 }
  useUserStore().permissions = new Set(['report:view', 'issue:view'])
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

it('只有报告概览权限时不开放包含问题证据的HTML预览或下载', async () => {
  useUserStore().permissions.delete('issue:view')
  await render()
  expect(button('打印')).toBeDefined()
  expect(button('导出报告')).toBeUndefined()
  expect(wrapper.find('[data-testid="report-preview-button"]').exists()).toBe(false)
  await state().handlePreview()
  await flushPromises()
  expect(api.previewReport).not.toHaveBeenCalled()
  expect(api.generateReport).not.toHaveBeenCalled()
  expect(api.exportReport).not.toHaveBeenCalled()
  expect(issues.getTaskIssues).not.toHaveBeenCalled()
})

it('缺少问题明细权限时明确说明权限边界，报告概览仍可查看', async () => {
  useUserStore().permissions.delete('issue:view')
  await render()
  expect(wrapper.get('[data-testid="report-issues-restricted"]').text()).toContain('当前账号没有问题明细查看权限')
  expect(wrapper.find('[data-testid="report-issues-retry"]').exists()).toBe(false)
  expect(issues.getTaskIssues).not.toHaveBeenCalled()
})

it('报告查看权限撤销后立即清除报告内容', async () => {
  await render()
  expect(wrapper.text()).toContain('账号专属报告摘要')

  useUserStore().permissions.delete('report:view')
  await flushPromises()

  expect(wrapper.find('[data-testid="report-access-revoked"]').exists()).toBe(true)
  expect(wrapper.find('.report-paper').exists()).toBe(false)
  expect(wrapper.text()).not.toContain('账号专属报告摘要')
  expect(wrapper.find('[data-testid="report-issues-restricted"]').exists()).toBe(false)
})

it('问题明细权限撤销后清空已读明细并保留概览权限提示', async () => {
  issues.getTaskIssues.mockResolvedValueOnce({
    items: [{
      id: 91, task_id: 42, issue_type: 'security', severity: 'critical', title: '其他权限下不可见的漏洞',
      description: '敏感问题说明', status: 'open', create_time: '2026-09-05T00:00:00Z',
      cvss_source: 'vector', cvss_version: '3.1', cvss_vector: 'AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H', cvss_score: 9.8,
    }],
    total: 1,
  })
  await render()
  expect(wrapper.text()).toContain('其他权限下不可见的漏洞')

  useUserStore().permissions.delete('issue:view')
  await flushPromises()

  expect(wrapper.find('[data-testid="report-issues-restricted"]').exists()).toBe(true)
  expect(wrapper.text()).not.toContain('其他权限下不可见的漏洞')
})

it('问题明细接口失败时保留主报告并提供可点击重试', async () => {
  useUserStore().permissions.add('issue:view')
  issues.getTaskIssues.mockRejectedValueOnce(new Error('temporary failure'))
  await render()
  expect(wrapper.get('[data-testid="report-issues-load-error"]').text()).toContain('问题明细暂时无法加载')
  expect(wrapper.find('[data-testid="report-issues-retry"]').exists()).toBe(true)
  issues.getTaskIssues.mockResolvedValueOnce({ items: [], total: 0 })
  await wrapper.get('[data-testid="report-issues-retry"]').trigger('click')
  await flushPromises()
  expect(issues.getTaskIssues).toHaveBeenCalledTimes(2)
  expect(wrapper.find('[data-testid="report-issues-load-error"]').exists()).toBe(false)
})

it('沙箱报告显示服务端记录的验证覆盖状态和原因', async () => {
  api.getReportDetail.mockResolvedValueOnce({
    ...report,
    task: { coverage: { verification_status: 'partial', requested_mode: 'combined', reason: 'AI 动态测试未执行' } },
    source: { type: 'sandbox_test' },
  })
  await render()
  const coverage = wrapper.get('[data-testid="sandbox-report-coverage"]')
  expect(coverage.text()).toContain('验证范围不完整')
  expect(coverage.text()).toContain('AI 动态测试未执行')
  expect(coverage.text()).toContain('不代表完整黑盒/白盒验证通过')
})

it('白盒回执完整时明确说明黑盒未执行，不把请求范围外测试写成已覆盖', async () => {
  api.getReportDetail.mockResolvedValueOnce({
    ...report,
    task: { coverage: {
      verification_status: 'complete', coverage_status: 'complete', requested_mode: 'whitebox',
      deterministic_status: 'passed', ai_dynamic_status: 'skipped',
      blackbox_status: 'not_required', remote_blackbox_status: 'not_required',
    } },
    source: { type: 'sandbox_test' },
  })
  await render()

  expect(wrapper.get('[data-testid="report-execution-status"]').text())
    .toContain('白盒验证完成；未执行黑盒或远程黑盒测试')
  const details = wrapper.get('[data-testid="report-coverage-details"]').text()
  expect(details).toContain('请求模式：白盒')
  expect(details).toContain('确定性检查：已通过')
  expect(details).toContain('AI 动态用例：未执行')
  expect(details).toContain('黑盒验证：未纳入本次验证')
  expect(details).toContain('远程黑盒验证：未纳入本次验证')
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

it.each(['sandbox_test', 'pentest'])('来源未加载时无操作；%s已加载后开放有权限的同源格式', async (type) => {
  for (const format of ['json', 'html', 'pdf', 'word']) useUserStore().permissions.add(`report:export:${format}`)
  if (type === 'pentest') useUserStore().permissions.add('pentest:view')
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
  expect(button('生成 HTML')).toBeDefined()
  expect(button('导出 PDF')).toBeDefined()
  expect(wrapper.find('[data-testid="report-preview-button"]').exists()).toBe(true)
  await state().handleExport('html')
  await state().downloadWord()
  expect(api.exportReport).toHaveBeenCalledTimes(2)
  if (type === 'sandbox_test') expect(wrapper.get('[data-testid="report-domain-score-label"]').text()).toContain('领域评分')
})
