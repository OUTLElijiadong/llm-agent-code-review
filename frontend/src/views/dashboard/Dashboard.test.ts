import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { computed, nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SummaryOut } from '@/types/dashboard'

const mocks = vi.hoisted(() => ({
  getSummary: vi.fn(),
  getRiskDistribution: vi.fn(),
  getIssueTypeStatistics: vi.fn(),
  getScoreTrend: vi.fn(),
  getReviewFrequency: vi.fn(),
  getRunning: vi.fn(),
  push: vi.fn(),
  message: { warning: vi.fn(), error: vi.fn(), success: vi.fn(), info: vi.fn() },
}))
const permissions = vi.hoisted(() => ({ canExport: true, exportFormat: 'html' }))

vi.mock('@/api/dashboard', () => mocks)
vi.mock('vue-router', () => ({ useRouter: () => ({ push: mocks.push }) }))
vi.mock('@/stores/user', () => ({
  useUserStore: () => ({ hasPermission: (code: string) => code !== 'security:view' && (!code.startsWith('report:export:') || (permissions.canExport && code === `report:export:${permissions.exportFormat}`)) }),
}))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: mocks.message }))
vi.mock('@/composables/useCountUp', () => ({ useCountUp: (source: { value: number }) => computed(() => source.value) }))
vi.mock('@/components/security/SecurityPostureCard.vue', () => ({ default: { template: '<div />' } }))

import Dashboard from './Dashboard.vue'

const wrappers: VueWrapper[] = []

function summary(overrides: Partial<SummaryOut> = {}): SummaryOut {
  return {
    project_count: 3,
    file_count: 12,
    review_count: 4,
    code_review_count: 4,
    total_issues: 9,
    severe_issues: 2,
    avg_score: 81,
    recent_tasks: [],
    ...overrides,
  }
}

function deferred<Value>() {
  let resolve!: (value: Value) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<Value>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function mountPage() {
  const wrapper = mount(Dashboard, {
    global: {
      stubs: {
        'el-button': { props: ['loading', 'disabled'], template: '<button :disabled="loading || disabled"><slot /></button>' },
        'el-select': { name: 'ElSelect', props: ['modelValue'], template: '<select><slot /></select>' },
        'el-option': { props: ['label', 'value'], template: '<option :value="value">{{ label }}</option>' },
        'el-icon': { template: '<span><slot /></span>' },
        'el-alert': { template: '<div><slot name="title" /><slot /></div>' },
        BaseChart: { props: ['option'], template: '<div class="chart-output">{{ JSON.stringify(option) }}</div>' },
        EmptyState: { props: ['description'], template: '<p>{{ description }}</p>' },
        PrismLoading: { props: ['label', 'sublabel'], template: '<div>{{ label }} {{ sublabel }}</div>' },
        FluidProgress: true,
        SecurityPostureCard: true,
        RouterLink: true,
      },
    },
  })
  wrappers.push(wrapper)
  return wrapper
}

function section(wrapper: VueWrapper, key: string) {
  return wrapper.get(`[data-section="${key}"]`)
}

async function changeRange(wrapper: VueWrapper, days: number) {
  const select = wrapper.getComponent({ name: 'ElSelect' })
  select.vm.$emit('update:modelValue', days)
  select.vm.$emit('change', days)
  await nextTick()
}

beforeEach(() => {
  permissions.canExport = true
  permissions.exportFormat = 'html'
  Object.values(mocks).forEach((mock) => { if (vi.isMockFunction(mock)) mock.mockReset() })
  mocks.getSummary.mockResolvedValue(summary())
  mocks.getRiskDistribution.mockResolvedValue([])
  mocks.getIssueTypeStatistics.mockResolvedValue([])
  mocks.getScoreTrend.mockResolvedValue([])
  mocks.getReviewFrequency.mockResolvedValue([])
  mocks.getRunning.mockResolvedValue({ reviews: [], agents: [] })
})

afterEach(() => { wrappers.splice(0).forEach((wrapper) => wrapper.unmount()) })

describe('成员仪表盘真实读取状态', () => {
  it('未知维度不得从图表和图例消失，分项合计应等于真实总数', async () => {
    mocks.getIssueTypeStatistics.mockResolvedValue([
      { issue_type: 'security', count: 30 },
      { issue_type: 'logic', count: 30 },
      { issue_type: '历史自定义类型', count: 200 },
      { issue_type: '另一个未映射类型', count: 35 },
    ])
    const wrapper = mountPage()
    await flushPromises()
    const dimension = section(wrapper, 'dimension')
    expect(dimension.text()).toContain('295 个')
    expect(dimension.get('.legend-list').text()).toContain('未归类235')
    const option = JSON.parse(dimension.get('.chart-output').text())
    expect(option.angleAxis.data).toContain('未归类')
    expect(option.series[0].data.reduce((total: number, item: { value: number }) => total + item.value, 0)).toBe(295)
  })

  it('全部问题未归类时显示真实数量而不是全零光谱', async () => {
    mocks.getIssueTypeStatistics.mockResolvedValue([{ issue_type: '未知分类', count: 7 }])
    const wrapper = mountPage()
    await flushPromises()
    const dimension = section(wrapper, 'dimension')
    expect(dimension.get('.legend-list').text()).toContain('未归类7')
    expect(dimension.text()).not.toContain('暂无维度数据')
  })

  it('首载未取得摘要时不显示零项任务或零分', async () => {
    mocks.getSummary.mockReturnValue(deferred<SummaryOut>().promise)
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('.stat-grid').exists()).toBe(false)
    expect(wrapper.find('.page-sub .hl').exists()).toBe(false)
    expect(section(wrapper, 'summary').attributes('aria-busy')).toBe('true')
    expect(wrapper.get('[data-testid="dashboard-progress"]').text()).toContain('4 / 5')
  })

  it('摘要失败显示可重试错误而不假报空业务', async () => {
    mocks.getSummary.mockRejectedValueOnce(new Error('database unavailable'))
    const wrapper = mountPage()
    await flushPromises()
    expect(section(wrapper, 'summary').text()).toContain('摘要读取失败')
    expect(wrapper.find('.stat-grid').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('暂无最近活动')
    expect(wrapper.find('.page-sub .hl').exists()).toBe(false)
    await section(wrapper, 'summary').get('button').trigger('click')
    await flushPromises()
    expect(mocks.getSummary).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.stat-grid').text()).toContain('81.0')
  })

  it('真实全零保留零值，没有已完成审查时不判为风险或零分', async () => {
    mocks.getSummary.mockResolvedValue(summary({
      project_count: 0, file_count: 0, review_count: 0, code_review_count: 0, total_issues: 0, severe_issues: 0, avg_score: 0,
    }))
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.findAll('.stat-num').map((item) => item.text())).toEqual(['0次', '0个', '0个', '—/100', '0个', '0份'])
    expect(wrapper.find('.stat-gauge').exists()).toBe(false)
    expect(wrapper.text()).toContain('暂无代码评分样本')
  })

  it('已有成功审查的真实零分不被当成未知', async () => {
    mocks.getSummary.mockResolvedValue(summary({ review_count: 1, code_review_count: 1, avg_score: 0 }))
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.findAll('.stat-num')[3].text()).toBe('0.0/100')
    expect(wrapper.find('.stat-gauge').text()).toContain('极高风险')
  })

  it('畸形摘要不得变成可导出的统计事实', async () => {
    mocks.getSummary.mockResolvedValue({ ...summary(), review_count: null })
    const wrapper = mountPage()
    await flushPromises()
    expect(section(wrapper, 'summary').text()).toContain('摘要读取失败')
    expect(wrapper.find('.stat-grid').exists()).toBe(false)
    expect(wrapper.get('[data-testid="export-dashboard"]').attributes('disabled')).toBeDefined()
  })

  it('部分接口迟迟未返回时成功分区仍立即展示', async () => {
    mocks.getRiskDistribution.mockReturnValue(deferred<never[]>().promise)
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('.stat-grid').text()).toContain('81.0')
    expect(section(wrapper, 'risk').attributes('aria-busy')).toBe('true')
    expect(section(wrapper, 'dimension').text()).toContain('暂无维度数据')
  })

  it('维度接口成功且为空不再永远显示汇总中或八个假零', async () => {
    const wrapper = mountPage()
    await flushPromises()
    expect(section(wrapper, 'dimension').text()).toContain('暂无维度数据')
    expect(section(wrapper, 'dimension').text()).not.toContain('正在汇总')
    expect(section(wrapper, 'dimension').find('.legend-list').exists()).toBe(false)
  })

  it('单图失败与空态分开且仅重试失败图', async () => {
    mocks.getRiskDistribution.mockRejectedValueOnce(new Error('risk unavailable'))
    const wrapper = mountPage()
    await flushPromises()
    expect(section(wrapper, 'risk').text()).toContain('严重度数据读取失败')
    expect(section(wrapper, 'risk').text()).not.toContain('暂无严重度数据')
    expect(wrapper.find('.stat-grid').exists()).toBe(true)
    await section(wrapper, 'risk').get('button').trigger('click')
    await flushPromises()
    expect(section(wrapper, 'risk').text()).toContain('暂无严重度数据')
    expect(mocks.getSummary).toHaveBeenCalledTimes(1)
    expect(mocks.getRiskDistribution).toHaveBeenCalledTimes(2)
    expect(mocks.getIssueTypeStatistics).toHaveBeenCalledTimes(1)
  })

  it('切换日期时旧区间成功响应不得覆盖新图', async () => {
    const oldRisk = deferred<{ severity: string; count: number }[]>()
    mocks.getRiskDistribution.mockImplementation((days: number) => days === 30 ? oldRisk.promise : Promise.resolve([{ severity: '严重', count: 7 }]))
    const wrapper = mountPage()
    await flushPromises()
    await changeRange(wrapper, 7)
    await flushPromises()
    expect(section(wrapper, 'risk').text()).toContain('"value":7')
    oldRisk.resolve([{ severity: '严重', count: 999 }])
    await flushPromises()
    expect(section(wrapper, 'risk').text()).toContain('"value":7')
    expect(section(wrapper, 'risk').text()).not.toContain('999')
    expect(mocks.getReviewFrequency.mock.calls.map((args) => args[0])).toEqual([30, 7])
  })

  it('旧区间失败不得覆盖新请求成功状态', async () => {
    const oldRisk = deferred<never[]>()
    mocks.getRiskDistribution.mockReturnValueOnce(oldRisk.promise).mockResolvedValue([])
    const wrapper = mountPage()
    await flushPromises()
    await changeRange(wrapper, 7)
    await flushPromises()
    oldRisk.reject(new Error('old failure'))
    await flushPromises()
    expect(section(wrapper, 'risk').attributes('data-state')).toBe('success')
    expect(section(wrapper, 'risk').text()).toContain('暂无严重度数据')
  })

  it('读取失败或仍在加载时禁止导出不完整报告', async () => {
    mocks.getIssueTypeStatistics.mockRejectedValue(new Error('unavailable'))
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.get('[data-testid="export-dashboard"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[data-testid="export-dashboard"]').trigger('click')
    expect(download).not.toHaveBeenCalled()
  })

  it('不展示写死的宣传、假直播标签和非周区间的周报文案', async () => {
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).not.toContain('v3.4 全链路源码审计已上线')
    expect(wrapper.text()).not.toContain('1M')
    expect(wrapper.text()).not.toContain('实时 · 流式')
    expect(wrapper.text()).not.toContain('TOP')
    expect(wrapper.text()).not.toContain('导出周报')
    expect(wrapper.text()).toContain('导出统计报告')
  })

  it('点击导出下载有文件名的HTML报告并反馈，不依赖弹出窗口', async () => {
    vi.useFakeTimers()
    class ReportURL extends URL {
      static createObjectURL = vi.fn(() => 'blob:https://review.example/report-test')
      static revokeObjectURL = vi.fn()
    }
    vi.stubGlobal('URL', ReportURL)
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const downloads: { href: string; filename: string; connected: boolean }[] = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      downloads.push({ href: this.href, filename: this.download, connected: this.isConnected })
    })
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.get('[data-testid="export-dashboard"]').trigger('click')
    expect(open).not.toHaveBeenCalled()
    expect(downloads).toEqual([{ href: 'blob:https://review.example/report-test', filename: expect.stringMatching(/^prism-statistics-30d-\d{8}-\d{6}\.html$/), connected: true }])
    expect(document.querySelector('a[download]')).toBeNull()
    expect(mocks.message.warning).not.toHaveBeenCalled()
    expect(mocks.message.info).toHaveBeenCalledWith(expect.stringContaining('已请求下载统计报告'))
    expect(ReportURL.revokeObjectURL).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(ReportURL.revokeObjectURL).toHaveBeenCalledWith('blob:https://review.example/report-test')
  })

  it('触发下载失败时给出反馈并清理临时链接与URL', async () => {
    class ReportURL extends URL {
      static createObjectURL = vi.fn(() => 'blob:https://review.example/report-test')
      static revokeObjectURL = vi.fn()
    }
    vi.stubGlobal('URL', ReportURL)
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => { throw new Error('download denied') })
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.get('[data-testid="export-dashboard"]').trigger('click')
    expect(mocks.message.error).toHaveBeenCalledWith(expect.stringContaining('无法下载统计报告'))
    expect(mocks.message.info).not.toHaveBeenCalled()
    expect(ReportURL.revokeObjectURL).toHaveBeenCalledWith('blob:https://review.example/report-test')
    expect(document.querySelector('a[download]')).toBeNull()
  })

  it('下载内容保留真实统计范围与数值，项目标题按文本转义', async () => {
    let report: Blob | undefined
    class ReportURL extends URL {
      static createObjectURL = vi.fn((blob: Blob) => { report = blob; return 'blob:https://review.example/data-check' })
      static revokeObjectURL = vi.fn()
    }
    vi.stubGlobal('URL', ReportURL)
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    mocks.getSummary.mockResolvedValue(summary({ recent_tasks: [{
      id: 7, task_name: '审查 <script>unsafe()</script>', project_id: 1,
      project_name: '皮卡丘 & 测试', status: 'success', score: 81, create_time: '2026-09-07T07:00:00',
    }] }))
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.get('[data-testid="export-dashboard"]').trigger('click')
    expect(report?.type).toBe('text/html;charset=utf-8')
    const html = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.onerror = reject
      reader.readAsText(report!)
    })
    expect(html).toContain('近 30 天；概览为累计值')
    expect(html).toContain('皮卡丘 &amp; 测试')
    expect(html).toContain('&lt;script&gt;unsafe()&lt;/script&gt;')
    expect(html).not.toContain('<script>')
    expect(html).toContain('<div class="n">9</div><div class="l">累计发现问题</div>')
  })

  it('后端真实四类零计数不画出虚假的等分饼图且允许导出真实零', async () => {
    mocks.getRiskDistribution.mockResolvedValue(['严重', '高', '中', '低'].map((severity) => ({ severity, count: 0 })))
    const wrapper = mountPage()
    await flushPromises()
    expect(section(wrapper, 'risk').attributes('data-state')).toBe('success')
    expect(section(wrapper, 'risk').find('.chart-output').exists()).toBe(false)
    expect(section(wrapper, 'risk').text()).toContain('暂无严重度数据')
    expect(wrapper.get('[data-testid="export-dashboard"]').attributes('disabled')).toBeUndefined()
  })

  it('窗口全零但累计有数据时提示口径并可一键切换累计', async () => {
    mocks.getRiskDistribution.mockResolvedValue(['严重', '高', '中', '低'].map((severity) => ({ severity, count: 0 })))
    const wrapper = mountPage()
    await flushPromises()
    const risk = section(wrapper, 'risk')
    expect(risk.text()).toContain('近 30 天暂无严重度数据')
    expect(risk.get('[data-testid="risk-cumulative-hint"]').text()).toContain('9')
    expect(mocks.getRiskDistribution).toHaveBeenCalledWith(30)

    mocks.getRiskDistribution.mockResolvedValue([
      { severity: '严重', count: 5 }, { severity: '高', count: 3 },
      { severity: '中', count: 2 }, { severity: '低', count: 1 },
    ])
    await risk.get('[data-testid="risk-cumulative-hint"] button').trigger('click')
    await flushPromises()
    expect(mocks.getRiskDistribution).toHaveBeenLastCalledWith(0)
    expect(section(wrapper, 'risk').find('.chart-output').exists()).toBe(true)
    expect(section(wrapper, 'risk').text()).toContain('累计的问题分布')
  })

  it.each([
    ['risk', null], ['risk', [null]], ['risk', [{ severity: '严重', count: -1 }]],
    ['risk', [{ severity: null, count: 1 }]], ['risk', [{ severity: '严重', count: Infinity }]],
    ['dimension', [{ issue_type: '安全漏洞', count: '3' }]], ['dimension', [{ issue_type: '安全漏洞', count: null }]],
    ['dimension', [{ issue_type: 42, count: 2 }]], ['dimension', [{ issue_type: '安全漏洞', count: NaN }]],
    ['score', [{ task_id: 1, score: 101, create_time: '2026-09-05T12:00:00' }]],
    ['score', [{ task_id: null, score: 50, create_time: '2026-09-05T12:00:00' }]],
    ['score', [{ task_id: 1, score: '50', create_time: '2026-09-05T12:00:00' }]],
    ['score', [{ task_id: 1, score: 50, create_time: '2026-02-30T12:00:00' }]],
    ['frequency', [{ date: 'invalid', count: 1 }]], ['frequency', [{ date: '2026-02-30', count: 1 }]],
    ['frequency', [{ date: '2026-09-05', count: -1 }]], ['frequency', [{ date: '2026-09-05', count: '2' }]],
  ])('%s非法载荷%j不能作为成功数据或导出来源', async (key, data) => {
    const requests = { risk: mocks.getRiskDistribution, dimension: mocks.getIssueTypeStatistics, score: mocks.getScoreTrend, frequency: mocks.getReviewFrequency }
    requests[key as keyof typeof requests].mockResolvedValue(data)
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const wrapper = mountPage()
    await flushPromises()
    expect(section(wrapper, String(key)).attributes('data-state')).toBe('error')
    expect(section(wrapper, String(key)).find('.chart-output').exists()).toBe(false)
    expect(wrapper.find('.stat-grid').exists()).toBe(true)
    expect(wrapper.get('[data-testid="export-dashboard"]').attributes('disabled')).toBeDefined()
    ;(wrapper.vm as unknown as { onWeeklyReport(): void }).onWeeklyReport()
    expect(download).not.toHaveBeenCalled()
  })

  it.each([
    null,
    { id: '2' },
    { task_name: 2 },
    { project_name: null },
    { score: NaN },
    { score: 101 },
    { status: 'pending' },
    { create_time: '2026-02-30T12:00:00' },
  ])('摘要非法任务成员%j被入口拒绝而非渲染崩溃', async (invalid) => {
    const task = { id: 2, task_name: '真实任务', project_id: 1, project_name: '真实项目', status: 'success', score: 80, create_time: null }
    mocks.getSummary.mockResolvedValue(summary({ recent_tasks: [invalid === null ? null : { ...task, ...invalid }] as never }))
    const wrapper = mountPage()
    await flushPromises()
    expect(section(wrapper, 'summary').attributes('data-state')).toBe('error')
    expect(wrapper.find('.stat-grid').exists()).toBe(false)
    expect(wrapper.get('[data-testid="export-dashboard"]').attributes('disabled')).toBeDefined()
  })

  it('无导出权限时按钮不可见且直接调用也不能打开报告', async () => {
    permissions.canExport = false
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('[data-testid="export-dashboard"]').exists()).toBe(false)
    ;(wrapper.vm as unknown as { onWeeklyReport(): void }).onWeeklyReport()
    expect(download).not.toHaveBeenCalled()
  })

  it('只有PDF导出权限时不能下载HTML格式', async () => {
    permissions.exportFormat = 'pdf'
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('[data-testid="export-dashboard"]').exists()).toBe(false)
    ;(wrapper.vm as unknown as { onWeeklyReport(): void }).onWeeklyReport()
    expect(download).not.toHaveBeenCalled()
  })

  it('加载中时按钮和直接导出调用均拒绝', async () => {
    mocks.getScoreTrend.mockReturnValue(deferred<never[]>().promise)
    const download = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.get('[data-testid="export-dashboard"]').attributes('disabled')).toBeDefined()
    ;(wrapper.vm as unknown as { onWeeklyReport(): void }).onWeeklyReport()
    expect(download).not.toHaveBeenCalled()
  })

  it('摘要重叠刷新不被旧请求覆盖', async () => {
    const oldSummary = deferred<SummaryOut>()
    mocks.getSummary.mockReturnValueOnce(oldSummary.promise).mockResolvedValue(summary({ avg_score: 95 }))
    const wrapper = mountPage()
    const controller = wrapper.vm as unknown as { loadSummary(): Promise<void> }
    await controller.loadSummary()
    await flushPromises()
    oldSummary.resolve(summary({ avg_score: 25 }))
    await flushPromises()
    expect(wrapper.findAll('.stat-num')[3].text()).toBe('95.0/100')
  })

  it('刷新失败隐藏旧图，成功摘要保留，单图重试恢复', async () => {
    mocks.getRiskDistribution.mockResolvedValueOnce([{ severity: '高', count: 9 }]).mockRejectedValueOnce(new Error('offline')).mockResolvedValue([{ severity: '高', count: 3 }])
    const wrapper = mountPage()
    await flushPromises()
    await changeRange(wrapper, 7)
    await flushPromises()
    expect(section(wrapper, 'risk').find('.chart-output').exists()).toBe(false)
    expect(wrapper.find('.stat-grid').text()).toContain('81.0')
    await section(wrapper, 'risk').get('button').trigger('click')
    await flushPromises()
    expect(section(wrapper, 'risk').text()).toContain('"value":3')
    expect(mocks.getSummary).toHaveBeenCalledTimes(1)
  })

  it('卸载后成功和失败都不写状态或留下任务刷新监听', async () => {
    const pendingSummary = deferred<SummaryOut>()
    const pendingRisk = deferred<never[]>()
    mocks.getSummary.mockReturnValue(pendingSummary.promise)
    mocks.getRiskDistribution.mockReturnValue(pendingRisk.promise)
    const removeListener = vi.spyOn(window, 'removeEventListener')
    const wrapper = mountPage()
    const controller = wrapper.vm as unknown as { summary: SummaryOut | null; summaryState: string; chartStates: { risk: string } }
    wrapper.unmount()
    wrappers.splice(wrappers.indexOf(wrapper), 1)
    pendingSummary.resolve(summary())
    pendingRisk.reject(new Error('late failure'))
    await flushPromises()
    expect(controller.summary).toBeNull()
    expect(controller.summaryState).toBe('loading')
    expect(controller.chartStates.risk).toBe('loading')
    expect(removeListener).toHaveBeenCalledWith('prism:agent-task-complete', expect.any(Function))
  })
})


describe('后台进度读取与恢复', () => {
  const activeReview = { id: 8, task_name: '正在审查', project_id: 2, project_name: '可见项目', review_type: 'standard', status: 'running', processed_files: 2, total_files: 5 }

  it('首屏实际请求后台进度并显示真实完成文件比例', async () => {
    mocks.getRunning.mockResolvedValue({ reviews: [activeReview], agents: [] })
    const wrapper = mountPage()
    await flushPromises()
    expect(mocks.getRunning).toHaveBeenCalledTimes(1)
    expect(wrapper.get('[data-testid="running-panel"]').text()).toContain('40%')
  })

  it('进度读取失败有独立重试且不伪装没有后台任务', async () => {
    mocks.getRunning.mockRejectedValueOnce(new Error('unavailable'))
    const wrapper = mountPage()
    await flushPromises()
    const panel = wrapper.get('[data-testid="running-panel"]')
    expect(panel.text()).toContain('后台进度读取失败')
    await panel.get('button').trigger('click')
    await flushPromises()
    expect(mocks.getRunning).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-testid="running-panel"]').exists()).toBe(false)
  })

  it('卸载后停止轮询，迟到响应不得重新启动轮询', async () => {
    vi.useFakeTimers()
    const response = deferred<{ reviews: typeof activeReview[]; agents: [] }>()
    mocks.getRunning.mockReturnValueOnce(response.promise)
    const wrapper = mountPage()
    await flushPromises()
    wrapper.unmount()
    response.resolve({ reviews: [activeReview], agents: [] })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(15000)
    expect(mocks.getRunning).toHaveBeenCalledTimes(1)
    vi.useRealTimers()
  })

  it('进行中刷新失败保留已确认进度并标为未更新', async () => {
    vi.useFakeTimers()
    mocks.getRunning.mockResolvedValueOnce({ reviews: [activeReview], agents: [] }).mockRejectedValue(new Error('offline'))
    const wrapper = mountPage()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    const panel = wrapper.get('[data-testid="running-panel"]')
    expect(panel.text()).toContain('40%')
    expect(panel.text()).toContain('后台进度读取失败')
    expect(panel.text()).toContain('上次读取结果')
    vi.useRealTimers()
  })

  it('纯空态仍定期重新发现其它页面启动的任务，避免永远隐藏', async () => {
    vi.useFakeTimers()
    mocks.getRunning.mockResolvedValueOnce({ reviews: [], agents: [] }).mockResolvedValue({ reviews: [activeReview], agents: [] })
    const wrapper = mountPage()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(30000)
    await flushPromises()
    expect(wrapper.get('[data-testid="running-panel"]').text()).toContain('40%')
    vi.useRealTimers()
  })

  it('非法进度载荷作为错误反馈而不崩溃或展示负进度', async () => {
    mocks.getRunning.mockResolvedValue({ reviews: [{ ...activeReview, processed_files: -2 }], agents: [] })
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.get('[data-testid="running-panel"]').text()).toContain('后台进度读取失败')
  })
})


describe('工作台补充失败边界', () => {
  it('同步抛错后仍允许重试，不能被已结束的请求引用锁死', async () => {
    mocks.getRunning.mockImplementationOnce(() => { throw new Error('sync failure') })
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.get('[data-testid="running-panel"] button').trigger('click')
    await flushPromises()
    expect(mocks.getRunning).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-testid="running-panel"]').exists()).toBe(false)
  })
  it('累计趋势实际请求累计，并保留跨年日期信息', async () => {
    const wrapper = mountPage()
    await flushPromises()
    mocks.getReviewFrequency.mockResolvedValue([{ date: '2025-09-01', count: 1 }, { date: '2026-09-01', count: 2 }])
    await changeRange(wrapper, 0)
    await flushPromises()
    expect(mocks.getReviewFrequency).toHaveBeenLastCalledWith(0)
    const option = JSON.parse(section(wrapper, 'frequency').get('.chart-output').text())
    expect(option.xAxis.data).toEqual(['2025/9/1', '2026/9/1'])
  })
  it('严重度计数不依赖画布悬浮提示而可以直接阅读', async () => {
    mocks.getRiskDistribution.mockResolvedValue([{ severity: '严重', count: 3 }, { severity: '未分级', count: 4 }])
    const wrapper = mountPage()
    await flushPromises()
    expect(section(wrapper, 'risk').get('.severity-values').text()).toContain('危急3')
    expect(section(wrapper, 'risk').get('.severity-values').text()).toContain('未分级4')
  })
})

describe('代码评分样本口径', () => {
  it('只有沙箱成功记录时不把测试100分显示成代码评分', async () => {
    mocks.getSummary.mockResolvedValue({ ...summary(), review_count: 1, code_review_count: 0, avg_score: 0,
      recent_tasks: [{ id: 1, task_name: '沙箱执行', project_id: 1, project_name: '项目', status: 'success',
        review_type: 'sandbox_test', score: 100, create_time: null }] })
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.findAll('.stat-num')[3].text()).toBe('—/100')
    expect(wrapper.find('.stat-gauge').exists()).toBe(false)
    expect(wrapper.text()).toContain('测试评分')
    expect(wrapper.text()).toContain('暂无代码评分样本')
  })

  it('缺失样本数或空分数不冒充有效代码评分', async () => {
    const legacy = { ...summary(), code_review_count: undefined, recent_tasks: [{ id: 1, task_name: '旧任务', project_id: 1, project_name: '项目',
      status: 'success', score: null, create_time: null }] }
    mocks.getSummary.mockResolvedValue(legacy)
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.findAll('.stat-num')[3].text()).toBe('—/100')
    expect(wrapper.text()).toContain('历史评分')
    expect(wrapper.text()).toContain('未提供')
  })
})
