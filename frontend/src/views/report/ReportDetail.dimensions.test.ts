import { flushPromises, shallowMount, type VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const route = vi.hoisted(() => ({ params: { id: '236' }, query: {} as Record<string, string> }))
const router = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn(), back: vi.fn() }))
const reportApi = vi.hoisted(() => ({
  getReportDetail: vi.fn(),
  generateReport: vi.fn(),
  previewReport: vi.fn(),
  exportReport: vi.fn(),
}))
const reviewApi = vi.hoisted(() => ({ getTaskIssues: vi.fn() }))
const permissions = vi.hoisted(() => new Set<string>())

vi.mock('vue-router', () => ({ useRoute: () => route, useRouter: () => router }))
vi.mock('@/stores/user', () => ({ useUserStore: () => ({ hasPermission: (code: string) => permissions.has(code) }) }))
vi.mock('@/api/report', () => reportApi)
vi.mock('@/api/review', () => reviewApi)
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { success: vi.fn(), error: vi.fn() } }))

import ReportDetail from './ReportDetail.vue'

type Dim = { key: string, label: string, score: number | null, status: string, reason: string }

const ORDER: Array<[string, string]> = [
  ['style', '代码规范'], ['naming', '命名规范'], ['comment', '注释完整性'], ['maintain', '可维护性'],
  ['performance', '性能问题'], ['exception', '异常处理'], ['logic', '潜在 Bug'], ['security', '安全漏洞'],
]
const NO_DATA = '本任务没有记录该维度的问题，缺少评分依据（不代表满分）'

function items(scores: Record<string, number>, reason = NO_DATA): Dim[] {
  return ORDER.map(([key, label]) => ({
    key, label, score: key in scores ? scores[key] : null,
    status: key in scores ? 'scored' : 'no_data', reason: key in scores ? '' : reason,
  }))
}

function dimensionScores(scores: Record<string, number>, overrides: Record<string, unknown> = {}) {
  return {
    version: 'dimension-severity-deduction-v1', available: true, items: items(scores), scores,
    scored_count: Object.keys(scores).length, no_data_count: 8 - Object.keys(scores).length,
    unmapped_issue_count: 1,
    basis: '各维度分按该维度已记录且未证伪问题的严重度扣分：100 − 15×严重 − 8×高 − 3×中 − 1×低。各维度分不能相加或平均得到整体评分。',
    ...overrides,
  }
}

function mountPage(): VueWrapper {
  return shallowMount(ReportDetail, {
    global: {
      stubs: {
        'el-button': { template: '<button><slot /></button>' },
        'el-dialog': true, 'el-dropdown': { template: '<div><slot /><slot name="dropdown" /></div>' },
        'el-dropdown-item': { template: '<div><slot /></div>' }, 'el-dropdown-menu': { template: '<div><slot /></div>' },
        'el-button-group': { template: '<div><slot /></div>' }, 'el-icon': { template: '<i><slot /></i>' },
        'el-option': true, 'el-select': true, 'el-statistic': true,
        BaseChart: { name: 'BaseChart', props: ['option'], template: '<div class="chart-stub" />' },
        EmptyState: { name: 'EmptyState', props: ['description'], template: '<div class="empty-stub">{{ description }}</div>' },
        PrismLoading: true,
      },
      directives: { loading: () => undefined },
    },
  })
}

function setupState(wrapper: VueWrapper): Record<string, any> {
  return (wrapper.vm as unknown as { $: { setupState: Record<string, any> } }).$.setupState
}

async function render(scores: Record<string, number>, stats: Record<string, unknown> = {}, task: Record<string, unknown> = {}) {
  reportApi.getReportDetail.mockResolvedValueOnce({
    project: {},
    task: { task_name: '任务236', review_type: 'discuss', status: 'success', score: 62, ...task },
    stats: { score: 62, total_issues: 12, dim_scores: scores, dimension_scores: dimensionScores(scores), ...stats },
    files: [], rules_snapshot: [],
  })
  const wrapper = mountPage()
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  route.query = {}
  permissions.clear()
  for (const code of ['report:view', 'issue:view']) permissions.add(code)
  reviewApi.getTaskIssues.mockResolvedValue({ items: [], total: 0 })
})

describe('报告 8 维度评分雷达（圆桌 #236）', () => {
  it('只画有数据的维度，并在图下注明其余维度无数据', async () => {
    const wrapper = await render({ maintain: 97, exception: 93, security: 70 })
    const state = setupState(wrapper)

    expect(wrapper.findComponent({ name: 'BaseChart' }).exists()).toBe(true)
    expect(state.radarOption.radar.indicator.map((entry: { name: string }) => entry.name))
      .toEqual(['可维护性', '异常处理', '安全漏洞'])
    expect(state.radarOption.series[0].data[0].value).toEqual([97, 93, 70])
    const note = wrapper.get('[data-testid="dimension-radar-note"]').text()
    expect(note).toContain('未绘制 5 个维度')
    for (const label of ['代码规范', '命名规范', '注释完整性', '性能问题', '潜在 Bug']) expect(note).toContain(label)
    expect(note).toContain('无数据')
    expect(wrapper.text()).not.toContain('暂无 8 维度数据')
    wrapper.unmount()
  })

  it('真实 0 分作为数据绘制，不被当成缺失', async () => {
    const wrapper = await render({ security: 0, exception: 80, style: 100 })
    const values = setupState(wrapper).radarOption.series[0].data[0].value

    expect(values).toHaveLength(3)
    expect(values).toContain(0)
    wrapper.unmount()
  })

  it('少于 3 个有数据的维度画不出雷达：给出原因与已有分数，而不是空白或假多边形', async () => {
    const wrapper = await render({ security: 70 })

    expect(wrapper.findComponent({ name: 'BaseChart' }).exists()).toBe(false)
    const note = wrapper.get('[data-testid="dimension-radar-note"]').text()
    expect(note).toContain('仅 1 个维度有数据')
    expect(note).toContain('安全漏洞 70')
    expect(note).toContain('未绘制 7 个维度')
    wrapper.unmount()
  })

  it('没有任何维度有数据时说明缺少依据，不显示成 0 分', async () => {
    const wrapper = await render({})

    expect(wrapper.findComponent({ name: 'BaseChart' }).exists()).toBe(false)
    const note = wrapper.get('[data-testid="dimension-radar-note"]').text()
    expect(note).toContain('没有任何维度有评分依据')
    expect(note).toContain('无数据，不是 0 分')
    // 任何维度名后面都不会跟着一个分数（例如“安全漏洞 0”）。
    expect(note).not.toMatch(/(代码规范|命名规范|注释完整性|可维护性|性能问题|异常处理|潜在 Bug|安全漏洞)\s*\d/)
    wrapper.unmount()
  })

  it('整体评分不可用时不画，并带出后端给出的原因', async () => {
    const wrapper = await render({}, {
      dimension_scores: dimensionScores({}, {
        available: false, items: items({}, '存在未分级问题，无法按严重度规则计分'),
      }),
    })

    expect(wrapper.findComponent({ name: 'BaseChart' }).exists()).toBe(false)
    expect(wrapper.get('[data-testid="dimension-radar-note"]').text()).toContain('存在未分级问题')
    wrapper.unmount()
  })

  it('旧响应只有 dim_scores 时仍按数据绘制，缺失维度同样不当作 0', async () => {
    const wrapper = await render({ style: 90, naming: 85, security: 60 }, { dimension_scores: undefined })
    const state = setupState(wrapper)

    expect(state.radarOption.series[0].data[0].value).toEqual([90, 85, 60])
    expect(wrapper.get('[data-testid="dimension-radar-note"]').text()).toContain('未绘制 5 个维度')
    wrapper.unmount()
  })

  it('说明维度分的计算依据，并声明不能相加得到整体评分', async () => {
    const wrapper = await render({ maintain: 97, exception: 93, security: 70 })

    const text = wrapper.get('[data-testid="code-dimension-radar"]').text()
    expect(text).toContain('严重度扣分')
    expect(text).toContain('不能相加或平均得到整体评分')
    wrapper.unmount()
  })

  it('领域报告不显示代码质量雷达', async () => {
    reportApi.getReportDetail.mockResolvedValueOnce({
      project: {}, task: { review_type: 'sandbox_test', status: 'success' },
      stats: {}, files: [], rules_snapshot: [], source: { type: 'sandbox_test' },
    })
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.find('[data-testid="code-dimension-radar"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
