import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import type { DirectiveBinding } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SecurityDashboardSummaryOut } from '@/types/security'

const mocks = vi.hoisted(() => ({
  getDashboard: vi.fn(),
  routerPush: vi.fn(),
  permissions: new Set<string>(),
}))

vi.mock('@/api/security', () => ({
  getSecurityDashboard: mocks.getDashboard,
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mocks.routerPush }),
}))

vi.mock('@/stores/user', () => ({
  useUserStore: () => ({
    hasPermission: (code: string) => mocks.permissions.has(code),
  }),
}))

import SecurityPostureCard from './SecurityPostureCard.vue'

const baseSummary: SecurityDashboardSummaryOut = {
  user_scope: 'self',
  project_count: 2,
  scanned_project_count: 2,
  avg_risk_score: 42,
  severe_issues_total: 1,
  high_issues_total: 2,
  medium_issues_total: 3,
  low_issues_total: 4,
  owasp_hotspots: [{ owasp: 'A03:2021-Injection', count: 3 }],
  top_risky_projects: [
    {
      project_id: 9,
      project_name: 'critical-project',
      risk_score: 35,
      severe_issues: 1,
      high_issues: 2,
    },
  ],
  trend: [
    { date: '2026-07-09', severe: 0, high: 1 },
    { date: '2026-07-10', severe: 1, high: 2 },
  ],
}

/** 把 v-loading 当前值写入 DOM，便于断言加载交互。 */
function writeLoadingState(element: HTMLElement, binding: DirectiveBinding): void {
  element.dataset.loading = String(Boolean(binding.value))
}

/** 挂载安全态势卡并提供 Element Plus 轻量桩。 */
function mountCard(days = 30): VueWrapper {
  return mount(SecurityPostureCard, {
    props: { days },
    global: {
      directives: {
        loading: {
          mounted: writeLoadingState,
          updated: writeLoadingState,
        },
      },
      stubs: {
        'el-icon': { template: '<span class="el-icon-stub"><slot /></span>' },
        'el-button': {
          template: '<button class="el-button-stub"><slot /></button>',
        },
        ArrowRight: true,
        FolderOpened: true,
        Lock: true,
      },
    },
  })
}

/** 创建由测试手动 resolve/reject 的 Promise。 */
function deferred<T>(): {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (reason?: unknown) => void
} {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

/** 重置组件依赖桩。 */
function resetComponentMocks(): void {
  mocks.getDashboard.mockReset()
  mocks.routerPush.mockReset()
  mocks.permissions.clear()
  mocks.permissions.add('project:view')
  mocks.permissions.add('project:create')
  mocks.permissions.add('security:scan')
  mocks.permissions.add('security:view')
}

beforeEach(resetComponentMocks)

describe('SecurityPostureCard', () => {
  it('exposes loading state until the dashboard request settles', async () => {
    /** 验证 onMounted 请求期间和完成后的 loading 指令状态。 */
    const request = deferred<SecurityDashboardSummaryOut>()
    mocks.getDashboard.mockReturnValue(request.promise)
    const wrapper = mountCard(14)
    await flushPromises()

    expect(wrapper.attributes('data-loading')).toBe('true')
    expect(mocks.getDashboard).toHaveBeenCalledWith(14)

    request.resolve(baseSummary)
    await flushPromises()
    expect(wrapper.attributes('data-loading')).toBe('false')
    expect(wrapper.find('.card-body').exists()).toBe(true)
  })

  it('renders an empty project state and navigates to project creation', async () => {
    /** 验证零项目空态与主要操作按钮。 */
    mocks.getDashboard.mockResolvedValue({
      ...baseSummary,
      project_count: 0,
      scanned_project_count: 0,
      avg_risk_score: null,
      owasp_hotspots: [],
      top_risky_projects: [],
      trend: [],
    })
    const wrapper = mountCard()
    await flushPromises()

    expect(wrapper.find('.state-empty').text()).toContain('还没有项目可分析')
    await wrapper.find('.state-empty button').trigger('click')
    expect(mocks.routerPush).toHaveBeenCalledWith('/projects')
  })

  it('renders an unscanned state and routes to the project list', async () => {
    /** 验证有项目但无扫描记录的半空态。 */
    mocks.getDashboard.mockResolvedValue({
      ...baseSummary,
      project_count: 3,
      scanned_project_count: 0,
      avg_risk_score: null,
      owasp_hotspots: [],
      top_risky_projects: [],
      trend: [],
    })
    const wrapper = mountCard()
    await flushPromises()

    expect(wrapper.find('.state-empty').text()).toContain('3 个项目还没做过安全审计')
    await wrapper.find('.state-empty button').trigger('click')
    expect(mocks.routerPush).toHaveBeenCalledWith('/projects')
  })

  it('hides unavailable project and scan actions for a read-only account', async () => {
    mocks.permissions.clear()
    mocks.getDashboard.mockResolvedValueOnce({
      ...baseSummary,
      project_count: 0,
      scanned_project_count: 0,
      avg_risk_score: null,
      owasp_hotspots: [],
      top_risky_projects: [],
      trend: [],
    })
    const emptyWrapper = mountCard()
    await flushPromises()
    expect(emptyWrapper.find('.state-empty button').exists()).toBe(false)
    emptyWrapper.unmount()

    mocks.getDashboard.mockResolvedValueOnce({
      ...baseSummary,
      project_count: 2,
      scanned_project_count: 0,
      avg_risk_score: null,
      owasp_hotspots: [],
      top_risky_projects: [],
      trend: [],
    })
    const unscannedWrapper = mountCard()
    await flushPromises()
    expect(unscannedWrapper.find('.state-empty button').exists()).toBe(false)
    unscannedWrapper.unmount()
  })

  it('shows a recoverable error and retries successfully', async () => {
    /** 验证失败提示、重试交互和重试后的正常内容。 */
    mocks.getDashboard
      .mockRejectedValueOnce(new Error('服务暂不可用'))
      .mockResolvedValueOnce(baseSummary)
    const wrapper = mountCard()
    await flushPromises()

    expect(wrapper.find('.state-error').text()).toContain('服务暂不可用')
    expect(wrapper.find('.state-error button').text()).toBe('重试')

    await wrapper.find('.state-error button').trigger('click')
    await flushPromises()
    expect(mocks.getDashboard).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.state-error').exists()).toBe(false)
    expect(wrapper.find('.card-body').exists()).toBe(true)
  })

  it('uses the fallback error message for non-Error rejections', async () => {
    /** 验证未知异常仍提供稳定中文错误文案。 */
    mocks.getDashboard.mockRejectedValue({})
    const wrapper = mountCard()
    await flushPromises()

    expect(wrapper.find('.state-error').text()).toContain('加载安全态势失败')
  })

  it('renders scores, trends, hotspots and navigation for populated data', async () => {
    /** 验证正常态数据展示、SVG 趋势和两类导航交互。 */
    mocks.getDashboard.mockResolvedValue(baseSummary)
    const wrapper = mountCard(30)
    await flushPromises()

    expect(wrapper.find('.score-num').text()).toBe('42')
    expect(wrapper.find('.score-num').attributes('style')).toContain('rgb(220, 73, 97)')
    expect(wrapper.find('.meta-row').text()).toContain('10 个安全问题')
    expect(wrapper.find('.hotspot-list').text()).toContain('A03:2021-Injection')
    expect(wrapper.find('.sparkline').exists()).toBe(true)
    expect(wrapper.find('.sparkline polyline').attributes('points')).toContain(',')
    expect(wrapper.find('.sparkline path').attributes('d')).toContain('M')
    expect(wrapper.find('.risky-name').text()).toBe('critical-project')

    await wrapper.find('.risky-item').trigger('click')
    await wrapper.find('.card-link').trigger('click')
    expect(mocks.routerPush).toHaveBeenNthCalledWith(1, '/projects/9')
    expect(mocks.routerPush).toHaveBeenNthCalledWith(2, '/security')
  })

  it('renders medium, healthy and unknown score branches without optional lists', async () => {
    /** 验证评分颜色边界、无热点/高风险项目与无趋势分支。 */
    mocks.getDashboard.mockResolvedValue({
      ...baseSummary,
      avg_risk_score: 60,
      owasp_hotspots: [],
      top_risky_projects: [],
      trend: [{ date: '2026-07-10', severe: 0, high: 0 }],
    })
    const wrapper = mountCard()
    await flushPromises()
    expect(wrapper.find('.score-num').attributes('style')).toContain('rgb(217, 168, 87)')
    expect(wrapper.text()).toContain('暂无 OWASP 命中')
    expect(wrapper.text()).toContain('所有项目都健康')
    expect(wrapper.find('.sparkline').exists()).toBe(false)

    mocks.getDashboard.mockResolvedValue({ ...baseSummary, avg_risk_score: 90 })
    const healthyWrapper = mountCard()
    await flushPromises()
    expect(healthyWrapper.find('.score-num').attributes('style')).toContain('rgb(79, 184, 122)')

    mocks.getDashboard.mockResolvedValue({ ...baseSummary, avg_risk_score: null })
    const unknownWrapper = mountCard()
    await flushPromises()
    expect(unknownWrapper.find('.score-num').text()).toBe('—')
  })
})
