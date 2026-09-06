import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { computed, nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  getSummary: vi.fn(), getRiskDistribution: vi.fn(), getIssueTypeStatistics: vi.fn(),
  getScoreTrend: vi.fn(), getReviewFrequency: vi.fn(),
}))
vi.mock('@/api/dashboard', () => api)
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('@/stores/user', () => ({ useUserStore: () => ({ hasPermission: (code: string) => code !== 'security:view' }) }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { warning: vi.fn(), error: vi.fn(), success: vi.fn() } }))
vi.mock('@/composables/useCountUp', () => ({ useCountUp: (source: { value: number }) => computed(() => source.value) }))
vi.mock('@/components/security/SecurityPostureCard.vue', () => ({ default: { template: '<div />' } }))
import Dashboard from '@/views/dashboard/Dashboard.vue'

const wrappers: VueWrapper[] = []
const validSummary = () => ({ project_count: 3, file_count: 12, review_count: 4, total_issues: 9, severe_issues: 2, avg_score: 81, recent_tasks: [] })
function deferred<Value>() {
  let resolve!: (value: Value) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<Value>((accept, fail) => { resolve = accept; reject = fail })
  return { promise, resolve, reject }
}
function page(errors: unknown[] = []) {
  const wrapper = mount(Dashboard, {
    global: {
      config: { warnHandler: () => {}, errorHandler: (error) => { errors.push(error) } },
      stubs: {
        'el-button': { props: ['loading', 'disabled'], template: '<button :disabled="loading || disabled"><slot /></button>' },
        'el-select': { name: 'ElSelect', props: ['modelValue'], template: '<select><slot /></select>' },
        'el-option': { props: ['label', 'value'], template: '<option :value="value">{{ label }}</option>' },
        'el-icon': { template: '<span><slot /></span>' },
        BaseChart: { props: ['option'], template: '<div class="chart-output">{{ JSON.stringify(option) }}</div>' },
        EmptyState: { props: ['description'], template: '<p>{{ description }}</p>' },
        PrismLoading: { props: ['label'], template: '<p>{{ label }}</p>' },
        FluidProgress: true, SecurityPostureCard: true,
      },
    },
  })
  wrappers.push(wrapper)
  return wrapper
}
function section(wrapper: VueWrapper, key: string) { return wrapper.get(`[data-section="${key}"]`) }
async function range(wrapper: VueWrapper, days: number) {
  const select = wrapper.getComponent({ name: 'ElSelect' })
  select.vm.$emit('update:modelValue', days)
  select.vm.$emit('change', days)
  await nextTick()
}
beforeEach(() => {
  Object.values(api).forEach((mock) => mock.mockReset())
  api.getSummary.mockResolvedValue(validSummary())
  api.getRiskDistribution.mockResolvedValue([])
  api.getIssueTypeStatistics.mockResolvedValue([])
  api.getScoreTrend.mockResolvedValue([])
  api.getReviewFrequency.mockResolvedValue([])
})
afterEach(() => { wrappers.splice(0).forEach((wrapper) => wrapper.unmount()) })

describe('独立有界反例与保护检查', () => {
  it('真实后端四类全零应显示空态而非普通饼图', async () => {
    api.getRiskDistribution.mockResolvedValue(['严重', '高', '中', '低'].map((severity) => ({ severity, count: 0 })))
    const wrapper = page()
    await flushPromises()
    expect(section(wrapper, 'risk').find('.chart-output').exists()).toBe(false)
    expect(section(wrapper, 'risk').text()).toContain('暂无严重度数据')
  })

  it('四图非法字段应各自错误且不可导出', async () => {
    api.getRiskDistribution.mockResolvedValue([{ severity: '严重', count: -1 }])
    api.getIssueTypeStatistics.mockResolvedValue([{ issue_type: 'security', count: '3' }])
    api.getScoreTrend.mockResolvedValue([{ task_id: 1, score: 101 }])
    api.getReviewFrequency.mockResolvedValue([{ date: 'not-a-date', count: -1 }])
    const wrapper = page()
    await flushPromises()
    const observed = {
      states: ['risk', 'dimension', 'score', 'frequency'].map((key) => section(wrapper, key).attributes('data-state')),
      exportDisabled: wrapper.get('[data-testid="export-dashboard"]').attributes('disabled') !== undefined,
    }
    expect(observed).toEqual({ states: ['error', 'error', 'error', 'error'], exportDisabled: true })
  })

  it('摘要最近任务非法成员应被入口拒绝而非渲染异常', async () => {
    api.getSummary.mockResolvedValue({ ...validSummary(), recent_tasks: [null] })
    const errors: unknown[] = []
    const wrapper = page(errors)
    await flushPromises()
    expect({ state: (wrapper.vm as any).summaryState, renderErrors: errors.length }).toEqual({ state: 'error', renderErrors: 0 })
  })

  it('日期刷新失败隐藏旧图并保留未失败摘要且仅单图重试', async () => {
    const nextRisk = deferred<never[]>()
    api.getRiskDistribution.mockResolvedValueOnce([{ severity: '严重', count: 77 }]).mockReturnValueOnce(nextRisk.promise).mockResolvedValue([])
    const wrapper = page()
    await flushPromises()
    expect(section(wrapper, 'risk').text()).toContain('77')
    await range(wrapper, 7)
    expect(section(wrapper, 'risk').text()).not.toContain('77')
    nextRisk.reject(new Error('synthetic failure'))
    await flushPromises()
    expect(section(wrapper, 'risk').attributes('data-state')).toBe('error')
    expect(section(wrapper, 'summary').attributes('data-state')).toBe('success')
    expect(wrapper.get('[data-testid="export-dashboard"]').attributes('disabled')).toBeDefined()
    await section(wrapper, 'risk').get('button').trigger('click')
    await flushPromises()
    expect(api.getSummary).toHaveBeenCalledTimes(1)
    expect(api.getRiskDistribution).toHaveBeenCalledTimes(3)
    expect(api.getIssueTypeStatistics).toHaveBeenCalledTimes(2)
  })

  it('摘要重叠刷新仅保留最新请求结果', async () => {
    const oldSummary = deferred<ReturnType<typeof validSummary>>()
    api.getSummary.mockReturnValueOnce(oldSummary.promise).mockResolvedValue({ ...validSummary(), review_count: 11 })
    const wrapper = page()
    await flushPromises()
    await (wrapper.vm as any).loadSummary()
    expect(wrapper.findAll('.stat-num')[0].text()).toBe('11次')
    oldSummary.resolve({ ...validSummary(), review_count: 99 })
    await flushPromises()
    expect(wrapper.findAll('.stat-num')[0].text()).toBe('11次')
  })

  it('卸载后在途成功和失败不写状态且移除事件监听', async () => {
    const pendingSummary = deferred<ReturnType<typeof validSummary>>()
    const pendingRisk = deferred<never[]>()
    api.getSummary.mockReturnValue(pendingSummary.promise)
    api.getRiskDistribution.mockReturnValue(pendingRisk.promise)
    const remove = vi.spyOn(window, 'removeEventListener')
    const wrapper = page()
    await flushPromises()
    const state = (wrapper.vm as any).$ .setupState
    wrapper.unmount()
    pendingSummary.resolve(validSummary())
    pendingRisk.reject(new Error('synthetic late failure'))
    await flushPromises()
    expect(state.summaryState).toBe('loading')
    expect(state.chartStates.risk).toBe('loading')
    expect(state.summary).toBeNull()
    expect(remove).toHaveBeenCalledWith('prism:agent-task-complete', expect.any(Function))
  })
})
