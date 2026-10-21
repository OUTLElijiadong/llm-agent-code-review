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
  it.each([['trusted', '规则计分：复算一致'], ['rescored', '规则计分：已校正']])('评分 %s 仅说明规则复算，不声称独立终审', async (status, label) => {
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: {},
      task: { status: 'success', score_audit: { status, verification_scope: 'score_only', independent_review: false } },
      stats: { score: 29, total_issues: 17 }, files: [], rules_snapshot: [],
    })
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain(label)
    expect(wrapper.text()).not.toContain('小菱终审')
    wrapper.unmount()
  })

  it('详情明确显示执行失败原因和未完成的验证覆盖', async () => {
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: {},
      task: {
        status: 'failed',
        error_message: '远程执行器启动失败',
        coverage: { verification_status: 'partial', reason: '没有发起网络请求' },
      },
      stats: {}, files: [], rules_snapshot: [],
    })
    const wrapper = mountPage()
    await flushPromises()
    const status = wrapper.get('[data-testid="report-execution-status"]')
    expect(status.text()).toContain('执行失败')
    expect(status.text()).toContain('远程执行器启动失败')
    expect(status.text()).toContain('部分完成')
    expect(status.text()).toContain('没有发起网络请求')
    wrapper.unmount()
  })

  it('历史沙箱报告耗时无法确认时明确显示未知，不展示环境存活时长', async () => {
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: { project_name: '沙箱项目' },
      task: {
        task_name: '白盒测试', review_type: 'sandbox_test', status: 'failed',
        duration_ms: null,
        coverage: { verification_status: 'failed' },
      },
      stats: { total_issues: 0 },
      files: [], rules_snapshot: [],
      source: {
        duration_note: '历史环境总运行时间不能代表本次测试耗时；单次耗时未可靠记录。',
        domain_report_md: '## 执行结果\\n测试未完成',
      },
    })
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.get('[data-testid="report-execution-status"]').text())
      .toContain('历史环境总运行时间不能代表本次测试耗时')
    expect(wrapper.text()).toContain('未可靠记录')
    expect(wrapper.text()).not.toContain('15795m21s')
    wrapper.unmount()
  })

  it('常规代码报告显式区分待核实问题与已确认漏洞', async () => {
    reviewApi.getTaskIssues.mockResolvedValueOnce({
      items: [{ ...issue, human_review_status: 'pending', evidence_quality: 'unsupported' }],
      total: 1,
    })
    const wrapper = mountPage()
    await flushPromises()

    const notice = wrapper.get('[data-testid="report-issue-verification"]')
    expect(notice.text()).toContain('1 项待人工核实')
    expect(notice.text()).toContain('包含待核实条目')
    expect(notice.text()).toContain('不证明漏洞条件或影响已成立')
    expect(wrapper.get('[data-testid="issue-review-status-7"]').text()).toBe('待人工核实')

    wrapper.unmount()
  })

  it.each([
    { task: { task_name: '项目167 完整代码审查（review_type=full）' }, expected: '项目167 完整代码审查' },
    { task: { name: '项目167 完整代码审查 (review_type=full)' }, expected: '项目167 完整代码审查' },
    { task: { task_name: '业务审查（review_type=experimental）' }, expected: '业务审查（review_type=experimental）' },
    { task: {}, expected: '任务 #42' },
  ])('报告封面复用任务展示名，保留未知业务后缀与原始数据：$expected', async ({ task, expected }) => {
    const original = { ...task }
    reportApi.getReportDetail.mockResolvedValueOnce({ task, project: {}, stats: {}, files: [], rules_snapshot: [] })
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.get('.cover-task').text()).toContain(expected)
    if (expected === '项目167 完整代码审查') expect(wrapper.get('.cover-task').text()).not.toContain('review_type=')
    expect(task).toEqual(original)
    wrapper.unmount()
  })

  it('历史沙箱通用任务名按实际模式展示且不重复审查类型', async () => {
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: { project_name: '历史测试项目' },
      task: {
        task_name: '沙箱黑白盒测试 · sbx_legacy',
        review_type: 'sandbox_test',
        coverage: { requested_mode: 'blackbox' },
      },
      stats: {}, files: [], rules_snapshot: [], source: { type: 'sandbox_test' },
    })
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.get('.cover-task').text()).toBe('沙箱黑盒测试 · sbx_legacy')
    wrapper.unmount()
  })

  it.each([
    { count: 2, total: 6, displayed: '33.3%' },
    { count: 1, total: 3, displayed: '33.3%' },
    { count: 1, total: 1, displayed: '100%' },
    { count: 0, total: 0, displayed: '0%' },
  ])('严重度占比 $count/$total 只在文字显示时四舍五入为 $displayed', async ({ count, total, displayed }) => {
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: {}, task: {}, stats: { total_issues: total, high: count }, files: [], rules_snapshot: [],
    })
    const wrapper = mountPage()
    await flushPromises()
    const high = wrapper.findAll('.sev-row').find(row => row.text().includes('高'))
    expect(high?.attributes('title')).toContain(`占比 ${displayed}`)
    expect(high?.text()).toContain(displayed)
    if (total) expect(setupState(wrapper).severityRows[1].percent).toBeCloseTo(count / total * 100)
    wrapper.unmount()
  })

  it('沙箱未分级与大数量报告仍用原值绘图，只缩短可见占比', async () => {
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: {}, task: {}, source: { type: 'sandbox_test' },
      stats: { total_issues: 7, severity: { 未分级: 2 } }, files: [], rules_snapshot: [],
    })
    const sandbox = mountPage()
    await flushPromises()
    const unclassified = sandbox.findAll('.sev-row').find(row => row.text().includes('未分级'))
    expect(unclassified?.attributes('title')).toContain('占比 28.6%')
    expect(unclassified?.text()).toContain('28.6%')
    expect(setupState(sandbox).severityRows[0].percent).toBeCloseTo(2 / 7 * 100)
    sandbox.unmount()

    reportApi.getReportDetail.mockResolvedValueOnce({
      project: {}, task: {}, stats: { total_issues: 1_000_000, high: 333_333 },
      files: [], rules_snapshot: [],
    })
    const large = mountPage()
    await flushPromises()
    const high = large.findAll('.sev-row').find(row => row.text().includes('高'))
    expect(high?.text()).toContain('33.3%')
    expect(setupState(large).severityRows[1].percent).toBeCloseTo(33.3333)
    large.unmount()
  })

  it.each(['2026-09-20T12:50:47', '2026-09-20T12:50:47Z', '2026-09-20T20:50:47+08:00'])('报告任务时间 %s 按 UTC 解释并保留本地生成时刻', async (createdAt) => {
    vi.stubEnv('TZ', 'Asia/Shanghai')
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-20T12:51:05Z'))
    reportApi.getReportDetail.mockResolvedValue({
      task: { create_time: createdAt }, project: {}, stats: {}, files: [], rules_snapshot: [],
    })
    const wrapper = mountPage()
    try {
      expect(new Date().getTimezoneOffset()).toBe(-480)
      await flushPromises()
      expect(wrapper.get('.cover-date').text()).toBe('2026-09-20 20:50')
      expect(wrapper.get('.paper-foot').text()).toContain('生成于 2026-09-20 20:51')
    } finally {
      wrapper.unmount()
      vi.unstubAllEnvs()
    }
  })

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
    expect(wrapper.get('[data-testid="report-domain-score-label"]').text()).toContain('领域评分')
    expect(wrapper.find('[data-testid="report-risk-level"]').exists()).toBe(false)
    expect(setupState(wrapper).severityRows).toHaveLength(1)
    wrapper.unmount()
  })

  it('沙箱报告明确区分 AI 审查未完成与执行验证范围', async () => {
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: { project_name: '沙箱项目' },
      task: { task_name: '沙箱黑白盒报告', review_type: 'sandbox_test', total_files: 1,
        coverage: { verification_status: 'partial', reason: '黑盒路由未配置' } },
      stats: { score: 60, total_issues: 0 },
      files: [], rules_snapshot: [],
      source: { type: 'sandbox_test', ai_review: { status: 'incomplete', failure_reason: '模型未配置' } },
    })
    const wrapper = mountPage()
    await flushPromises()
    const status = wrapper.get('[data-testid="sandbox-ai-review-status"]')
    expect(status.text()).toContain('多 Agent AI 报告：未完成（已提供确定性报告）')
    expect(status.text()).toContain('模型未配置')
    expect(wrapper.get('[data-testid="sandbox-report-coverage"]').text()).toContain('黑盒路由未配置')
    expect(wrapper.get('[data-testid="sandbox-report-coverage"]').text()).toContain('评分仅表示本次已记录范围的结果')
    wrapper.unmount()
  })

  it('沙箱没有有效评分时不显示局部评分说明，并明确标为未评估', async () => {
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: { project_name: '沙箱项目' },
      task: {
        task_name: '未评分的黑盒测试',
        review_type: 'sandbox_test',
        coverage: { verification_status: 'partial', reason: '未运行黑盒路由验证', score_available: false },
      },
      stats: { score: null, total_issues: 0 },
      files: [],
      rules_snapshot: [],
      source: { type: 'sandbox_test' },
    })
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.get('[data-testid="report-score-unavailable"]').text()).toContain('未评估')
    expect(wrapper.get('[data-testid="sandbox-report-coverage"]').text()).not.toContain('评分仅表示本次已记录范围的结果')
    wrapper.unmount()
  })

  it('策略跳过的多 Agent AI 审查显示为已跳过并保留原因', async () => {
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: {}, task: { review_type: 'sandbox_test' }, stats: {}, files: [], rules_snapshot: [],
      source: {
        type: 'sandbox_test',
        ai_review: { status: 'skipped', skip_reason: '精确 HTTP 白名单任务按策略不启动多 Agent AI 评审。' },
      },
    })
    const wrapper = mountPage()
    await flushPromises()
    const status = wrapper.get('[data-testid="sandbox-ai-review-status"]')
    expect(status.text()).toContain('多 Agent AI 报告：按任务策略跳过')
    expect(status.text()).toContain('精确 HTTP 白名单任务按策略不启动多 Agent AI 评审。')
    expect(status.text()).not.toContain('状态未知')
    wrapper.unmount()
  })

  it('旧沙箱报告未保存 AI 状态时标为未知，不声称报告审查完成', async () => {
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: {}, task: { review_type: 'sandbox_test' }, stats: {}, files: [], rules_snapshot: [],
      source: { type: 'sandbox_test' },
    })
    const wrapper = mountPage()
    await flushPromises()
    const status = wrapper.get('[data-testid="sandbox-ai-review-status"]')
    expect(status.text()).toContain('多 Agent AI 报告：状态未知')
    expect(status.text()).toContain('不能据此确认多 Agent AI 审查已完成')
    wrapper.unmount()
  })

  it('领域报告显示同源正文、预览和全部授权导出格式', async () => {
    permissions.add('pentest:view')
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: { project_name: '渗透项目', language: 'unknown' },
      task: { task_name: '渗透报告', review_type: 'pentest', total_files: 0 },
      stats: { score: 0, total_issues: 2 }, summary: '重复的领域报告摘要', files: [], rules_snapshot: [],
      source: { type: 'pentest', stats_basis: 'pentest_findings', domain_report_md: '# 渗透报告\n\n真实发现与修复建议' },
    })
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.get('[data-testid="domain-report-body"]').text()).toContain('真实发现与修复建议')
    expect(wrapper.find('.ai-card').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('重复的领域报告摘要')
    expect(wrapper.find('[data-testid="report-export-word"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="report-export-pdf"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('生成 JSON')
    expect(wrapper.text()).toContain('生成 HTML')
    expect(wrapper.text()).toContain('生成 PDF')
    expect(wrapper.text()).toContain('生成 Word')
    expect(wrapper.text()).toContain('预览 HTML')
    expect(wrapper.find('[data-testid="domain-report-metrics"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="code-repair-progress"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="code-dimension-radar"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="code-review-files"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('缺少问题明细权限时保留报告概览，但不提供包含明细的预览或导出', async () => {
    permissions.delete('issue:view')
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: { project_name: '权限测试项目', language: 'python' },
      task: { task_name: '代码审查', review_type: 'security', total_files: 1 },
      stats: { score: 72, total_issues: 1 }, files: [], rules_snapshot: [],
    })
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('报告概览仍可查看')
    expect(wrapper.find('[data-testid="report-preview-button"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="report-export-pdf"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('生成 JSON')
    expect(wrapper.text()).not.toContain('生成 HTML')
    expect(wrapper.text()).not.toContain('生成 PDF')
    expect(wrapper.text()).not.toContain('生成 Word')
    wrapper.unmount()
  })

  it('无领域来源权限时不显示领域报告正文、卡片或导出入口', async () => {
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: { project_name: '私有渗透项目' },
      task: { task_name: '私有渗透报告', review_type: 'pentest' },
      stats: {}, files: [], rules_snapshot: [],
      source: { type: 'pentest', domain_report_md: '# 不得泄露的报告正文' },
    })
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('[data-testid="domain-report-body"]').exists()).toBe(false)
    expect(wrapper.find('.report-paper').exists()).toBe(false)
    expect(wrapper.find('[data-testid="report-export-word"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="report-export-pdf"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('不得泄露的报告正文')
    expect(wrapper.text()).not.toContain('私有渗透报告')
    wrapper.unmount()
  })

  it('失败报告详情显示真实终态、错误原因与覆盖范围', async () => {
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: { project_name: '评估项目' },
      task: {
        task_name: '远程网站评估', review_type: 'remote_pentest', status: 'failed',
        error_message: '后台执行器启动失败；未发出网络请求。',
        coverage: { coverage_status: 'not_started', unscanned: ['根路径安全响应头检查'] },
      },
      stats: { total_issues: 0 }, summary: '报告已保存，未发出网络请求。', files: [], rules_snapshot: [],
      source: { type: 'remote_pentest' },
    })
    const wrapper = mountPage()
    await flushPromises()
    const execution = wrapper.get('[data-testid="report-execution-status"]')
    expect(execution.text()).toContain('执行失败')
    expect(execution.text()).toContain('未发出网络请求')
    expect(execution.text()).toContain('根路径安全响应头检查')
    expect(execution.text()).toContain('未开始执行')
    wrapper.unmount()
  })

  it('代码审查详情正确汇总文件覆盖并列出未扫描、排除与失败文件', async () => {
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: { project_name: '覆盖验证项目' },
      task: {
        task_name: '代码审查', review_type: 'security', status: 'success',
        coverage: {
          coverage_status: 'partial',
          completed_files: 2,
          total_files: 5,
          unscanned_files: 1,
          unscanned_file_names: ['src/queued.py'],
          excluded_files: [{ file_name: 'vendor/lib.js', reason: '第三方依赖' }],
          failed_files: ['src/broken.py'],
        },
      },
      stats: { total_issues: 0 }, files: [], rules_snapshot: [],
    })
    const wrapper = mountPage()
    await flushPromises()

    const execution = wrapper.get('[data-testid="report-execution-status"]')
    expect(execution.text()).toContain('部分完成')
    expect(execution.text()).toContain('文件处理 2/5')
    expect(execution.text()).toContain('未扫描文件：src/queued.py')
    expect(execution.text()).toContain('排除文件：vendor/lib.js（第三方依赖）')
    expect(execution.text()).toContain('失败文件：src/broken.py')
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

  it('顶部快捷导出和下方格式菜单共享并发锁，不会重复发送同格式导出', async () => {
    let resolveExport!: (value: Blob) => void
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    reportApi.exportReport.mockReturnValueOnce(new Promise((resolve) => { resolveExport = resolve }))
    const wrapper = mountPage()
    await flushPromises()
    const vm = setupState(wrapper)

    const topAction = vm.downloadWord()
    const menuAction = vm.handleExport('word')
    await flushPromises()

    expect(reportApi.exportReport).toHaveBeenCalledExactlyOnceWith(42, 'word', 'detailed')
    expect(vm.exportingFormat).toBe('word')
    resolveExport(new Blob(['report']))
    await Promise.all([topAction, menuAction])
    expect(click).toHaveBeenCalledTimes(1)
    click.mockRestore()
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

  it('问题明细超过单页上限时逐页读取完整结果，不静默截断统计', async () => {
    const firstItems = Array.from({ length: 1000 }, (_, index) => ({ ...issue, id: index + 1 }))
    const lastItem = { ...issue, id: 1001, title: '最后一页问题' }
    reviewApi.getTaskIssues
      .mockResolvedValueOnce({ items: firstItems, total: 1001, page: 1, page_size: 1000, pages: 2 })
      .mockResolvedValueOnce({ items: [lastItem], total: 1001, page: 2, page_size: 1000, pages: 2 })
    const wrapper = mountPage()
    await flushPromises()

    expect(reviewApi.getTaskIssues).toHaveBeenNthCalledWith(1, 42, { page: 1, page_size: 1000, status: 'all' })
    expect(reviewApi.getTaskIssues).toHaveBeenNthCalledWith(2, 42, { page: 2, page_size: 1000, status: 'all' })
    expect(setupState(wrapper).issues).toHaveLength(1001)
    expect(wrapper.findAll('[data-testid^="report-issue-item-"]')).toHaveLength(20)
    expect(wrapper.text()).not.toContain('最后一页问题')
    setupState(wrapper).setIssueDisplayPage(51)
    await flushPromises()
    expect(wrapper.findAll('[data-testid^="report-issue-item-"]')).toHaveLength(1)
    expect(wrapper.text()).toContain('最后一页问题')
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

  it('首次分数落地且后续风险文案和颜色不受动画中间值影响', async () => {
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(() => 1)
    const result = (score: number) => ({
      project: { project_name: '测试项目', language: 'typescript' },
      task: { name: '风险口径验证', review_type: 'security', total_files: 1 },
      stats: { score, total_issues: 0 },
      files: [],
      rules_snapshot: [],
    })
    reportApi.getReportDetail.mockReset()
      .mockResolvedValueOnce(result(80))
      .mockResolvedValueOnce(result(60))
    const wrapper = mountPage()
    await flushPromises()

    expect(setupState(wrapper).animatedScore).toBe(80)
    await setupState(wrapper).loadReport()
    await flushPromises()

    const risk = wrapper.find('[data-testid="report-risk-level"]')
    expect(setupState(wrapper).score).toBe(60)
    expect(setupState(wrapper).animatedScore).toBe(80)
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
    expect(wrapper.find('[data-testid="report-preview-button"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('生成、预览和导出包含问题证据的代码审查报告需要该权限')
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
    permissions.add('issue:view')
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

  it('自动生成等待领域报告来源就绪后生成 HTML 报告', async () => {
    route.query = { generate: '1' }
    let resolveReport!: (value: unknown) => void
    reportApi.getReportDetail.mockReturnValueOnce(new Promise(resolve => { resolveReport = resolve }))
    const wrapper = mountPage()
    await flushPromises()
    expect(reportApi.generateReport).not.toHaveBeenCalled()
    resolveReport({ project: {}, task: {}, stats: {}, files: [], rules_snapshot: [], source: { type: 'sandbox_test' } })
    await flushPromises()
    expect(reportApi.generateReport).toHaveBeenCalledExactlyOnceWith(42, 'html', 'detailed')
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
