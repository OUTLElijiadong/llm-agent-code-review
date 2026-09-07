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
  push: vi.fn(),
  message: { warning: vi.fn(), error: vi.fn(), success: vi.fn(), info: vi.fn() },
}))
const permissions = vi.hoisted(() => ({ canExport: true }))

vi.mock('@/api/dashboard', () => mocks)
vi.mock('vue-router', () => ({ useRouter: () => ({ push: mocks.push }) }))
vi.mock('@/stores/user', () => ({
  useUserStore: () => ({ hasPermission: (code: string) => code !== 'security:view' && (!code.startsWith('report:export:') || permissions.canExport) }),
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
  Object.values(mocks).forEach((mock) => { if (vi.isMockFunction(mock)) mock.mockReset() })
  mocks.getSummary.mockResolvedValue(summary())
  mocks.getRiskDistribution.mockResolvedValue([])
  mocks.getIssueTypeStatistics.mockResolvedValue([])
  mocks.getScoreTrend.mockResolvedValue([])
  mocks.getReviewFrequency.mockResolvedValue([])
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
      project_count: 0, file_count: 0, review_count: 0, total_issues: 0, severe_issues: 0, avg_score: 0,
    }))
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.findAll('.stat-num').map((item) => item.text())).toEqual(['0次', '0个', '0个', '—/100', '0个', '0份'])
    expect(wrapper.find('.stat-gauge').exists()).toBe(false)
    expect(wrapper.text()).toContain('暂无已完成审查')
  })

  it('已有成功审查的真实零分不被当成未知', async () => {
    mocks.getSummary.mockResolvedValue(summary({ review_count: 1, avg_score: 0 }))
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
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.get('[data-testid="export-dashboard"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[data-testid="export-dashboard"]').trigger('click')
    expect(open).not.toHaveBeenCalled()
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

  it('noopener返回null不被误报为拦截，也不立即回收已请求打开的报告', async () => {
    vi.useFakeTimers()
    class ReportURL extends URL {
      static createObjectURL = vi.fn(() => 'blob:https://review.example/report-test')
      static revokeObjectURL = vi.fn()
    }
    vi.stubGlobal('URL', ReportURL)
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.get('[data-testid="export-dashboard"]').trigger('click')
    expect(open).toHaveBeenCalledWith('blob:https://review.example/report-test', '_blank', 'noopener,noreferrer')
    expect(mocks.message.warning).not.toHaveBeenCalled()
    expect(mocks.message.info).toHaveBeenCalledWith(expect.stringContaining('已请求打开统计报告'))
    expect(ReportURL.revokeObjectURL).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(ReportURL.revokeObjectURL).toHaveBeenCalledWith('blob:https://review.example/report-test')
  })

  it('打开报告抛错时给出失败反馈并释放临时URL', async () => {
    class ReportURL extends URL {
      static createObjectURL = vi.fn(() => 'blob:https://review.example/report-test')
      static revokeObjectURL = vi.fn()
    }
    vi.stubGlobal('URL', ReportURL)
    vi.spyOn(window, 'open').mockImplementation(() => { throw new Error('window denied') })
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.get('[data-testid="export-dashboard"]').trigger('click')
    expect(mocks.message.error).toHaveBeenCalledWith(expect.stringContaining('无法打开统计报告'))
    expect(mocks.message.info).not.toHaveBeenCalled()
    expect(ReportURL.revokeObjectURL).toHaveBeenCalledWith('blob:https://review.example/report-test')
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
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const wrapper = mountPage()
    await flushPromises()
    expect(section(wrapper, String(key)).attributes('data-state')).toBe('error')
    expect(section(wrapper, String(key)).find('.chart-output').exists()).toBe(false)
    expect(wrapper.find('.stat-grid').exists()).toBe(true)
    expect(wrapper.get('[data-testid="export-dashboard"]').attributes('disabled')).toBeDefined()
    ;(wrapper.vm as unknown as { onWeeklyReport(): void }).onWeeklyReport()
    expect(open).not.toHaveBeenCalled()
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
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('[data-testid="export-dashboard"]').exists()).toBe(false)
    ;(wrapper.vm as unknown as { onWeeklyReport(): void }).onWeeklyReport()
    expect(open).not.toHaveBeenCalled()
  })

  it('加载中时按钮和直接导出调用均拒绝', async () => {
    mocks.getScoreTrend.mockReturnValue(deferred<never[]>().promise)
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.get('[data-testid="export-dashboard"]').attributes('disabled')).toBeDefined()
    ;(wrapper.vm as unknown as { onWeeklyReport(): void }).onWeeklyReport()
    expect(open).not.toHaveBeenCalled()
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
