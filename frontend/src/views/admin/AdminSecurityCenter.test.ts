import { defineComponent, onMounted } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import AdminSecurityCenter from './AdminSecurityCenter.vue'

const api = vi.hoisted(() => ({
  getSecurityCenterOverview: vi.fn(), getSecurityCenterEvents: vi.fn(), runSecurityMonitor: vi.fn(),
  updateSecurityMonitorPolicy: vi.fn(), getSystemStatus: vi.fn(), resolveAlert: vi.fn(),
  messageSuccess: vi.fn(), messageWarning: vi.fn(), messageError: vi.fn(), prompt: vi.fn(),
  blockingMount: vi.fn(), blockingRefresh: vi.fn(),
  blockingState: vi.fn(),
}))
const mountedPages: ReturnType<typeof mount>[] = []
const blockingStub = defineComponent({
  emits: ['status', 'changed'],
  setup(_, { emit, expose }) {
    expose({ refresh: api.blockingRefresh })
    onMounted(() => {
      api.blockingMount()
      emit('status', { snapshot: api.blockingState(), loading: false, error: '' })
    })
    return {}
  },
  template: '<section data-testid="automatic-blocking"><label>封禁草稿<input value="保留的草稿"></label></section>',
})
function mountPage() {
  const wrapper = mount(AdminSecurityCenter, { attachTo: document.body, global: { stubs: { AutomaticBlockingPanel: blockingStub } } })
  mountedPages.push(wrapper)
  return wrapper
}
vi.mock('@/api/adminSecurityCenter', () => ({
  getSecurityCenterOverview: api.getSecurityCenterOverview, getSecurityCenterEvents: api.getSecurityCenterEvents,
  runSecurityMonitor: api.runSecurityMonitor, updateSecurityMonitorPolicy: api.updateSecurityMonitorPolicy,
}))
vi.mock('@/api/adminOverview', () => ({ getSystemStatus: api.getSystemStatus }))
vi.mock('@/api/adminGovernance', () => ({ resolveAlert: api.resolveAlert }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { success: api.messageSuccess, warning: api.messageWarning, error: api.messageError } }))
vi.mock('element-plus/es/components/message-box/index', () => ({ ElMessageBox: { prompt: api.prompt } }))

function overview() {
  return {
    generated_at: '2026-10-05T12:00:00Z',
    monitoring: {
      enabled: true, schedule: '*/5 * * * *', schedule_label: '每 5 分钟', interval_minutes: 5,
      last_run: { status: 'success', started_at: '2026-10-05T11:55:00Z', finished_at: '2026-10-05T11:55:12Z', completed_sources: 4, failed_sources: 0, degraded_sources: 0, degraded: false },
      sources: [{ code: 'nginx_attack_events', label: '读取 Nginx 请求日志', status: 'success' as const }],
    },
    policy: {
      ssh_failed_threshold: 20, ssh_window_hours: 1, nginx_failure_threshold: 20, nginx_window_hours: 1,
      popup_min_severity: 'warning' as const, monitoring_mode: 'monitor_only' as const,
      automatic_blocking_enabled: false, counterattack_enabled: false, source: 'environment', revision: 0,
      updated_at: null, updated_by: null,
      baseline: { ssh_failed_threshold: 20, ssh_window_hours: 1, nginx_failure_threshold: 20, nginx_window_hours: 1, popup_min_severity: 'warning' as const },
    },
    open_alerts_24h: 1, open_alerts_total: 1,
  }
}
function eventPage(title = 'Nginx 重复 HTTP 异常') {
  return {
    items: [{ id: 'alert:9', alert_id: 9, recorded_at: '2026-10-05T11:50:00Z', event_type: 'alert', layer: '规则告警', severity: 'warning', status: 'open', actor: '安全监控规则', title,
      summary: '异常请求达到规则阈值。', action_code: 'security.rule.alert',
      evidence_summary: { ip: '203.0.113.9', failure_count: 20, threshold: 20, confirmed: true }, resolution: null }],
    total: 1, page: 1, page_size: 20, pages: 1, truncated: false, hours: 24,
  }
}
async function openEvents(wrapper: ReturnType<typeof mountPage>) {
  await wrapper.get('#security-tab-events').trigger('click')
  await flushPromises()
}
async function openStrategies(wrapper: ReturnType<typeof mountPage>) {
  await wrapper.get('#security-tab-strategies').trigger('click')
  await flushPromises()
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

describe('管理员安全中心', () => {
  afterEach(() => { mountedPages.splice(0).forEach((wrapper) => wrapper.unmount()); vi.useRealTimers() })
  beforeEach(() => {
    vi.clearAllMocks()
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-05T12:00:00Z'))
    api.getSecurityCenterOverview.mockResolvedValue(overview())
    api.getSecurityCenterEvents.mockResolvedValue(eventPage())
    api.blockingRefresh.mockResolvedValue(true)
    api.blockingState.mockReturnValue({ available: true, verified: true, enabled: false })
    api.getSystemStatus.mockResolvedValue({ available: true, collected_at: '2026-10-05T12:00:00Z', process_uptime_seconds: 10, cpu_percent: 4, memory_percent: 35, disk_percent: 48, disk_used_gb: 48, disk_total_gb: 100, uptime_seconds: 86400 })
  })

  it('默认展示安全概览及服务端最近活动，并移除两段旧声明', async () => {
    const wrapper = mountPage(); await flushPromises()
    expect(wrapper.get('#security-tab-overview').attributes('aria-selected')).toBe('true')
    expect(wrapper.get('#security-panel-overview').isVisible()).toBe(true)
    expect(wrapper.get('#security-panel-events').isVisible()).toBe(false)
    expect(wrapper.get('#security-panel-strategies').isVisible()).toBe(false)
    expect(wrapper.text()).toContain('小菱安全中心')
    expect(wrapper.text()).toContain('自动封禁：关闭')
    expect(wrapper.text()).not.toContain('证据边界')
    expect(wrapper.text()).not.toContain('日志覆盖范围')
    expect(api.getSecurityCenterEvents).toHaveBeenCalledExactlyOnceWith(24, 1, 5, 'activity')
    await wrapper.get('[data-testid="view-all-events"]').trigger('click'); await flushPromises()
    expect(wrapper.get('#security-panel-events').isVisible()).toBe(true)
    expect(api.getSecurityCenterEvents).toHaveBeenLastCalledWith(24, 1, 20, 'activity')
  })

  it('支持方向键及 Home、End 切换，保持焦点与面板关联', async () => {
    const wrapper = mountPage(); await flushPromises()
    await wrapper.get('#security-tab-overview').trigger('keydown', { key: 'ArrowRight' }); await flushPromises()
    expect(wrapper.get('#security-tab-events').attributes('aria-selected')).toBe('true')
    expect(document.activeElement?.id).toBe('security-tab-events')
    await wrapper.get('#security-tab-events').trigger('keydown', { key: 'End' })
    expect(wrapper.get('#security-tab-strategies').attributes('aria-selected')).toBe('true')
    expect(wrapper.get('#security-tab-strategies').attributes('aria-controls')).toBe('security-panel-strategies')
    await wrapper.get('#security-tab-strategies').trigger('keydown', { key: 'Home' })
    expect(wrapper.get('#security-panel-overview').attributes('aria-labelledby')).toBe('security-tab-overview')
    expect(document.activeElement?.id).toBe('security-tab-overview')
  })

  it('封禁回执结果未知时页头保持待核验，不宣称已启用', async () => {
    api.blockingState.mockReturnValue({ available: true, verified: true, enabled: true, outcome_unknown: true })
    const wrapper = mountPage(); await flushPromises()
    expect(wrapper.get('.hero-badges').text()).toContain('执行状态待核验')
    expect(wrapper.get('.hero-badges').text()).toContain('监控与告警')
    expect(wrapper.get('.hero-badges').text()).not.toContain('已启用')
  })

  it('数据源快捷入口请求巡检分组，并把键盘焦点带到事件页签', async () => {
    const wrapper = mountPage(); await flushPromises()
    await wrapper.get('.source-inspection').trigger('click'); await flushPromises()
    expect(api.getSecurityCenterEvents).toHaveBeenLastCalledWith(24, 1, 20, 'inspection')
    expect(wrapper.get('#security-panel-events').isVisible()).toBe(true)
    expect(document.activeElement?.id).toBe('security-tab-events')
  })

  it('切换页签保留封禁组件和草稿，且不重复获取事件', async () => {
    const wrapper = mountPage(); await flushPromises(); await openStrategies(wrapper)
    await wrapper.get('[data-testid="automatic-blocking"] input').setValue('已编辑')
    await openEvents(wrapper)
    await wrapper.get('#security-tab-overview').trigger('click')
    await openEvents(wrapper); await openStrategies(wrapper)
    expect(api.blockingMount).toHaveBeenCalledOnce()
    expect(api.getSecurityCenterEvents).toHaveBeenCalledTimes(2)
    expect((wrapper.get('[data-testid="automatic-blocking"] input').element as HTMLInputElement).value).toBe('已编辑')
  })

  it('按服务端分组与时间查询，重置分页并清除失败查询的旧列表', async () => {
    const wrapper = mountPage(); await flushPromises(); await openEvents(wrapper)
    await wrapper.get('[data-testid="event-group"]').setValue('inspection'); await flushPromises()
    expect(api.getSecurityCenterEvents).toHaveBeenLastCalledWith(24, 1, 20, 'inspection')
    api.getSecurityCenterEvents.mockRejectedValueOnce(new Error('网络超时'))
    await wrapper.get('[data-testid="event-range"]').setValue(168); await flushPromises()
    const panel = wrapper.get('#security-panel-events')
    expect(panel.text()).toContain('事件记录读取失败：网络超时')
    expect(panel.text()).not.toContain('Nginx 重复 HTTP 异常')
    expect(api.getSecurityCenterEvents).toHaveBeenLastCalledWith(168, 1, 20, 'inspection')
    await panel.get('[data-testid="retry-events"]').trigger('click'); await flushPromises()
    expect(panel.text()).toContain('Nginx 重复 HTTP 异常')
  })

  it('快速切换范围时只呈现最后一次查询，概览仍显示自己的最近活动', async () => {
    const wrapper = mountPage(); await flushPromises(); await openEvents(wrapper)
    const older = deferred<ReturnType<typeof eventPage>>(); const latest = deferred<ReturnType<typeof eventPage>>()
    api.getSecurityCenterEvents.mockReturnValueOnce(older.promise).mockReturnValueOnce(latest.promise)
    await wrapper.get('[data-testid="event-range"]').setValue(168)
    await wrapper.get('[data-testid="event-group"]').setValue('all')
    latest.resolve(eventPage('最新范围事件')); await flushPromises()
    older.resolve(eventPage('过期范围事件')); await flushPromises()
    expect(wrapper.get('#security-panel-events').text()).toContain('最新范围事件')
    expect(wrapper.get('#security-panel-events').text()).not.toContain('过期范围事件')
    expect(wrapper.get('#security-panel-overview').text()).toContain('Nginx 重复 HTTP 异常')
  })

  it('全页刷新与筛选重叠时，事件和概览查询分别隔离', async () => {
    const wrapper = mountPage(); await flushPromises(); await openEvents(wrapper)
    const recent = deferred<ReturnType<typeof eventPage>>(); const older = deferred<ReturnType<typeof eventPage>>(); const latest = deferred<ReturnType<typeof eventPage>>()
    api.getSecurityCenterEvents.mockImplementation((_hours, _page, size, group) => size === 5 ? recent.promise : group === 'activity' ? older.promise : latest.promise)
    await wrapper.get('[data-testid="refresh-all"]').trigger('click')
    await wrapper.get('[data-testid="event-group"]').setValue('inspection')
    latest.resolve(eventPage('新采集记录')); older.resolve(eventPage('旧活动记录')); recent.resolve(eventPage('新概览活动')); await flushPromises()
    expect(wrapper.get('#security-panel-events').text()).toContain('新采集记录')
    expect(wrapper.get('#security-panel-events').text()).not.toContain('旧活动记录')
    expect(wrapper.get('#security-panel-overview').text()).toContain('新概览活动')
  })

  it('分页使用服务端总数，筛选返回第一页且超出末页时回到有效页', async () => {
    api.getSecurityCenterEvents.mockImplementation((_hours, current, size) => Promise.resolve({ ...eventPage(`第 ${current} 页`), total: 41, pages: 3, page: current, page_size: size }))
    const wrapper = mountPage(); await flushPromises(); await openEvents(wrapper)
    await wrapper.get('[data-testid="next-page"]').trigger('click'); await flushPromises()
    expect(api.getSecurityCenterEvents).toHaveBeenLastCalledWith(24, 2, 20, 'activity')
    api.getSecurityCenterEvents.mockImplementation((_hours, current, size) => Promise.resolve({ ...eventPage(`第 ${current} 页`), total: 1, pages: 1, page: current, page_size: size, items: current > 1 ? [] : eventPage().items }))
    await wrapper.get('[data-testid="next-page"]').trigger('click'); await flushPromises()
    expect(api.getSecurityCenterEvents).toHaveBeenLastCalledWith(24, 1, 20, 'activity')
    expect(wrapper.get('.pager').text()).toContain('1 / 1')
  })

  it('事件证据默认折叠，状态译为人话且内部动作只出现在详情', async () => {
    const wrapper = mountPage(); await flushPromises(); await openEvents(wrapper)
    const detail = wrapper.get('#security-panel-events .event-details')
    expect(detail.attributes('open')).toBeUndefined()
    expect(wrapper.get('#security-panel-events .event-topline').text()).not.toContain('security.rule.alert')
    ;(detail.get('summary').element as HTMLElement).click()
    expect((detail.element as HTMLDetailsElement).open).toBe(true)
    expect(detail.text()).toContain('203.0.113.9')
    expect(detail.text()).toContain('已确认')
    expect(detail.text()).toContain('security.rule.alert')
  })

  it('配置布尔值与采集截断按含义翻译', async () => {
    api.getSecurityCenterEvents.mockResolvedValue({ ...eventPage(), items: [{ ...eventPage().items[0], evidence_summary: { confirmed: true, enabled: false, counterattack_enabled: false, source_truncated: false, routine_check: true } }] })
    const wrapper = mountPage(); await flushPromises(); await openEvents(wrapper)
    const detail = wrapper.get('#security-panel-events .event-details')
    expect(detail.text()).toContain('已关闭')
    expect(detail.text()).toContain('未截断')
    expect(detail.text()).toContain('例行核验')
    expect(detail.text()).toContain('已确认')
  })

  it('告警处理保留说明，并刷新活动与概览统计', async () => {
    api.prompt.mockResolvedValue({ value: '已核验' }); api.resolveAlert.mockResolvedValue({})
    const wrapper = mountPage(); await flushPromises(); await openEvents(wrapper)
    await wrapper.get('#security-panel-events .resolve-button').trigger('click'); await flushPromises()
    expect(api.resolveAlert).toHaveBeenCalledWith(9, '已核验')
    expect(api.getSecurityCenterOverview).toHaveBeenCalledTimes(2)
    expect(api.messageSuccess).toHaveBeenCalledWith('告警已处理，原始证据已保留。')
  })

  it('保留失败和降级来源名称，并在概览显示采集状态', async () => {
    const result = overview()
    api.getSecurityCenterOverview.mockResolvedValue({ ...result, monitoring: { ...result.monitoring, last_run: { ...result.monitoring.last_run, failed_sources: 1, degraded_sources: 1, degraded: true }, sources: [
      { code: 'ssh_login_events', label: '读取 SSH 登录日志', status: 'failed' },
      { code: 'nginx_attack_events', label: '读取 Nginx 请求日志', status: 'degraded' },
    ] } })
    const wrapper = mountPage(); await flushPromises()
    expect(wrapper.text()).toContain('2 个来源失败或降级')
    expect(wrapper.get('.source-list').text()).toContain('读取 SSH 登录日志')
    expect(wrapper.get('.source-list').text()).toContain('降级')
  })

  it('概览读取失败显示未知并提供重试，旧统计不伪装成当前结果', async () => {
    const wrapper = mountPage(); await flushPromises()
    api.getSecurityCenterOverview.mockRejectedValueOnce(new Error('访问被拒绝'))
    await wrapper.get('[data-testid="refresh-all"]').trigger('click'); await flushPromises()
    expect(wrapper.get('#security-panel-overview').text()).toContain('安全状态读取失败：访问被拒绝')
    expect(wrapper.get('.metrics-grid').text()).toContain('状态未知')
    expect(wrapper.get('.metrics-grid').text()).not.toContain('近期巡检正常')
    expect(wrapper.get('.policy-draft-state').text()).toBe('策略状态待核验')
    await wrapper.get('[data-testid="retry-overview"]').trigger('click'); await flushPromises()
    expect(wrapper.get('.metrics-grid').text()).toContain('近期巡检正常')
  })

  it('对过期运行、缺少完成时间与持续停留时的过期保持准确状态', async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-05T12:00:00Z'))
    const result = overview()
    api.getSecurityCenterOverview.mockResolvedValue({ ...result, monitoring: { ...result.monitoring, last_run: { ...result.monitoring.last_run, status: 'running', started_at: '2026-10-05T11:00:00Z', finished_at: null } } })
    const wrapper = mountPage(); await flushPromises()
    expect(wrapper.text()).toContain('巡检超过预期'); expect(wrapper.text()).not.toContain('巡检进行中')
    api.getSecurityCenterOverview.mockResolvedValue(overview())
    await wrapper.get('[data-testid="refresh-all"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('近期巡检正常')
    await vi.advanceTimersByTimeAsync(16 * 60_000)
    expect(wrapper.text()).toContain('巡检记录已过期')
    api.getSecurityCenterOverview.mockResolvedValue({ ...result, monitoring: { ...result.monitoring, last_run: { ...result.monitoring.last_run, finished_at: null } } })
    await wrapper.get('[data-testid="refresh-all"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('巡检记录不完整')
  })

  it('资源空值显示未知、0 值显示合法进度和运行时长', async () => {
    api.getSystemStatus.mockResolvedValue({ available: true, collected_at: '2026-10-05T12:00:00Z', cpu_percent: null, memory_percent: 0, disk_percent: 0, uptime_seconds: 0 })
    const wrapper = mountPage(); await flushPromises()
    expect(wrapper.get('[data-testid="resource-cpu"]').text()).toContain('未知')
    expect(wrapper.get('[data-testid="resource-cpu"]').text()).not.toContain('未知%')
    expect(wrapper.get('[data-testid="resource-cpu"]').find('progress').exists()).toBe(false)
    expect(wrapper.get('[data-testid="resource-memory"]').get('progress').attributes('value')).toBe('0')
    expect(wrapper.get('[data-testid="resource-uptime"]').text()).toContain('0 分钟')
  })

  it('资源读取失败保留重试入口，并隐藏旧的使用率进度', async () => {
    const wrapper = mountPage(); await flushPromises()
    api.getSystemStatus.mockRejectedValueOnce(new Error('采集超时'))
    await wrapper.get('[data-testid="refresh-all"]').trigger('click'); await flushPromises()
    expect(wrapper.get('.runtime-panel').text()).toContain('运行环境资源读取失败：采集超时')
    expect(wrapper.get('.runtime-panel').find('progress').exists()).toBe(false)
    await wrapper.get('[data-testid="retry-server"]').trigger('click'); await flushPromises()
    expect(wrapper.get('.runtime-panel').find('progress').exists()).toBe(true)
  })

  it('刷新及切页签保留未保存的监控草稿，重置采用最新服务端配置', async () => {
    const wrapper = mountPage(); await flushPromises(); await openStrategies(wrapper)
    await wrapper.get('[name="ssh_failed_threshold"]').setValue(10)
    expect(wrapper.get('.policy-draft-state').text()).toContain('未保存修改')
    api.getSecurityCenterOverview.mockResolvedValue({ ...overview(), policy: { ...overview().policy, ssh_failed_threshold: 15 } })
    await wrapper.get('[data-testid="refresh-all"]').trigger('click'); await flushPromises()
    await openEvents(wrapper); await openStrategies(wrapper)
    expect((wrapper.get('[name="ssh_failed_threshold"]').element as HTMLInputElement).value).toBe('10')
    await wrapper.get('[data-testid="reset-policy"]').trigger('click')
    expect((wrapper.get('[name="ssh_failed_threshold"]').element as HTMLInputElement).value).toBe('15')
    expect(wrapper.get('[data-testid="save-policy"]').attributes('disabled')).toBeDefined()
  })

  it('保存仅提交现有四个字段，并反馈成功或失败', async () => {
    api.updateSecurityMonitorPolicy.mockResolvedValue({ ...overview().policy, ssh_failed_threshold: 10, revision: 1, source: 'database' })
    const wrapper = mountPage(); await flushPromises(); await openStrategies(wrapper)
    await wrapper.get('[name="ssh_failed_threshold"]').setValue(10)
    await wrapper.get('.policy-form').trigger('submit'); await flushPromises()
    expect(api.updateSecurityMonitorPolicy).toHaveBeenCalledExactlyOnceWith({ ssh_failed_threshold: 10, ssh_window_hours: 1, nginx_failure_threshold: 20, nginx_window_hours: 1 })
    expect(wrapper.get('.policy-save-feedback').text()).toContain('已保存')
    expect(wrapper.get('[name="ssh_failed_threshold"]').attributes('max')).toBe('10')
    await wrapper.get('[name="ssh_failed_threshold"]').setValue(5)
    api.updateSecurityMonitorPolicy.mockRejectedValueOnce(new Error('保存超时'))
    await wrapper.get('.policy-form').trigger('submit'); await flushPromises()
    expect(wrapper.get('.policy-save-feedback').text()).toContain('保存超时')
    expect((wrapper.get('[name="ssh_failed_threshold"]').element as HTMLInputElement).value).toBe('5')
  })

  it('立即巡检刷新真实记录并报告数据源异常', async () => {
    api.runSecurityMonitor.mockResolvedValue({ success: false, created_alerts: [], errors: [{ action: 'ssh', error: 'timeout' }] })
    const wrapper = mountPage(); await flushPromises()
    await wrapper.get('[data-testid="run-monitor"]').trigger('click'); await flushPromises()
    expect(api.runSecurityMonitor).toHaveBeenCalledOnce()
    expect(api.getSecurityCenterOverview).toHaveBeenCalledTimes(2)
    expect(api.messageWarning).toHaveBeenCalledWith(expect.stringContaining('1 项'))
  })
})
