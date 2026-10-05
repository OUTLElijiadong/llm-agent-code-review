import { defineComponent, onMounted } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import AdminSecurityCenter from './AdminSecurityCenter.vue'

const api = vi.hoisted(() => ({
  getSecurityCenterOverview: vi.fn(), getSecurityCenterEvents: vi.fn(), runSecurityMonitor: vi.fn(),
  updateSecurityMonitorPolicy: vi.fn(), getSystemStatus: vi.fn(), resolveAlert: vi.fn(),
  traceSecurityIp: vi.fn(), getDefenseSurface: vi.fn(), getTrafficSummary: vi.fn(),
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
  traceSecurityIp: api.traceSecurityIp, getDefenseSurface: api.getDefenseSurface, getTrafficSummary: api.getTrafficSummary,
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
async function openTrace(wrapper: ReturnType<typeof mountPage>) {
  await wrapper.get('#security-tab-trace').trigger('click')
  await flushPromises()
}
function traceSnapshot(overrides: Record<string, unknown> = {}) {
  return {
    available: true, verified: true, kind: 'ip_trace', request_id: 'req-trace-1', ip: '203.0.113.9',
    generated_at: '2026-10-05T11:59:00Z', window_hours: 24, is_public: true, is_protected: false,
    risk: { score: 86, level: 'high', reasons: ['SSH 认证失败 42 次', '命中 3 个敏感路径'], basis: '本机 SSH 认证日志与 Nginx 访问日志' },
    ssh: { count: 42, accounts_tried: [{ account: 'root', count: 30 }, { account: 'admin', count: 12 }], first_seen: '2026-10-05T09:00:00Z', last_seen: '2026-10-05T11:30:00Z' },
    web: { count: 7, target_count: 3, targets: [{ path: '/.env', count: 4 }, { path: '/wp-login.php', count: 3 }], methods: { GET: 6, POST: 1 }, status_codes: { 403: 5, 404: 2 }, first_seen: '2026-10-05T09:10:00Z', last_seen: '2026-10-05T11:20:00Z' },
    defense_records: [{ id: 'blk-1', ip: '203.0.113.9', rule: 'ssh_failed_password', source: 'automatic_blocking', status: 'expired', started_at: '2026-10-05T09:30:00Z', expires_at: '2026-10-05T09:45:00Z', released_at: null, evidence_count: 21, reason: '短窗口重复认证失败' }],
    attribution: { ok: true, country: '德国', region: '黑森州', city: '法兰克福', isp: 'Example ISP', org: 'Example Org', as: 'AS64500' },
    reverse_dns: { ok: false, output: '', note: '未配置反向解析' },
    whois: { ok: true, summary: 'Example Org · DE', note: '' },
    evidence_sources: ['ssh 认证失败日志', 'nginx 访问日志'], errors: [],
    ...overrides,
  }
}
function surfaceSnapshot(overrides: Record<string, unknown> = {}) {
  return {
    available: true, verified: true, kind: 'surface_audit', request_id: 'req-surface-1', generated_at: '2026-10-05T11:58:00Z',
    listeners: [
      { protocol: 'tcp', address: '0.0.0.0', port: 443, process: 'nginx' },
      { protocol: 'tcp', address: '127.0.0.1', port: 8000, process: 'python' },
    ],
    public_listener_count: 1,
    firewall: {
      ipv4: {
        tool: 'iptables',
        chains: [
          { chain: 'INPUT', ok: true, policy: 'DROP', rules: ['-A INPUT -p tcp --dport 443 -j ACCEPT'], note: '' },
          { chain: 'DOCKER-USER', ok: false, policy: '未知', rules: [], note: '需要 root 权限' },
        ],
        tools_present: ['iptables', 'ipset'],
      },
      ipv6: { tool: 'ip6tables', chains: [{ chain: 'INPUT', ok: true, policy: 'DROP', rules: [], note: '' }], tools_present: ['ip6tables'] },
    },
    applications: [
      { name: 'fail2ban', purpose: '登录失败自动封禁', installed: true },
      { name: 'auditd', purpose: '系统调用审计', installed: false },
    ],
    ipset: { present: true, sets: ['prism_block_v4'] },
    blocking: { enabled: true, backend: 'ipset', active_leases: 2 },
    ssh_ports: '2222', errors: [],
    ...overrides,
  }
}
function trafficSnapshot(overrides: Record<string, unknown> = {}) {
  return {
    available: true, verified: true, kind: 'traffic_summary', request_id: 'req-traffic-1', generated_at: '2026-10-05T11:57:00Z', window_hours: 24,
    peers: [{
      ip: '203.0.113.9', connections: 3, protocols: { tcp: 3 }, peer_ports: { 443: 2, 22: 1 }, processes: ['nginx'],
      states: { ESTABLISHED: 2, SYN_SENT: 1 }, ssh_failed_count: 42, sensitive_probe_count: 7, target_count: 3, last_seen: '2026-10-05T11:30:00Z',
    }],
    peer_total: 5, current_connections: 9, recent_ssh_failed_sources: 2, recent_probe_sources: 1,
    payload_captured: false, note: '数据来自连接元数据与日志计数。', errors: [],
    ...overrides,
  }
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
    api.traceSecurityIp.mockResolvedValue(traceSnapshot())
    api.getDefenseSurface.mockResolvedValue(surfaceSnapshot())
    api.getTrafficSummary.mockResolvedValue(trafficSnapshot())
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
    await wrapper.get('#security-tab-events').trigger('keydown', { key: 'End' }); await flushPromises()
    expect(wrapper.get('#security-tab-trace').attributes('aria-selected')).toBe('true')
    expect(wrapper.get('#security-tab-trace').attributes('aria-controls')).toBe('security-panel-trace')
    expect(document.activeElement?.id).toBe('security-tab-trace')
    await wrapper.get('#security-tab-trace').trigger('keydown', { key: 'Home' })
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

  it('合法 IP 溯源成功渲染风险、证据、处置记录与外部归因标注', async () => {
    const wrapper = mountPage(); await flushPromises()
    expect(wrapper.get('#security-panel-trace').isVisible()).toBe(false)
    await openTrace(wrapper)
    expect(wrapper.get('#security-panel-trace').isVisible()).toBe(true)
    await wrapper.get('[data-testid="trace-ip-input"]').setValue('203.0.113.9')
    await wrapper.get('[data-testid="trace-ip-submit"]').trigger('click'); await flushPromises()
    expect(api.traceSecurityIp).toHaveBeenCalledExactlyOnceWith('203.0.113.9')
    const result = wrapper.get('[data-testid="trace-result"]')
    expect(result.text()).toContain('203.0.113.9')
    expect(result.text()).toContain('高风险')
    expect(result.get('.trace-risk-score').text()).toBe('86')
    expect(result.text()).toContain('观察窗口 24 小时')
    expect(result.text()).toContain('SSH 认证失败 42 次')
    expect(result.text()).toContain('root')
    expect(result.text()).toContain('/.env')
    expect(result.text()).toContain('3 个不同目标')
    expect(result.text()).toContain('GET × 6')
    expect(result.get('[data-testid="trace-risk-basis"]').text()).toContain('本机 SSH 认证日志与 Nginx 访问日志')
    expect(result.get('[data-testid="trace-risk-basis"]').text()).toContain('评分只使用本机可信日志')
    expect(result.get('[data-testid="trace-attribution-note"]').text()).toBe('外部被动归因，不参与评分')
    expect(result.text()).toContain('德国')
    expect(result.text()).toContain('法兰克福')
    expect(result.text()).toContain('Example ISP')
    expect(result.text()).toContain('AS64500')
    expect(result.text()).toContain('ssh_failed_password')
    expect(result.text()).toContain('已到期')
    expect(result.text()).toContain('短窗口重复认证失败')
    expect(result.text()).not.toContain('受保护来源，不参与自动处置')
    expect(wrapper.get('[data-testid="trace-readonly"]').text()).toContain('不向目标发送任何扫描或探测请求')
  })

  it('非法 IP 与网段只给行内提示，不发起溯源请求', async () => {
    const wrapper = mountPage(); await flushPromises(); await openTrace(wrapper)
    await wrapper.get('[data-testid="trace-ip-input"]').setValue('203.0.113.0/24')
    await wrapper.get('.trace-form').trigger('submit'); await flushPromises()
    expect(wrapper.get('[data-testid="trace-ip-error"]').text()).toContain('网段')
    await wrapper.get('[data-testid="trace-ip-input"]').setValue('not-an-ip')
    await wrapper.get('.trace-form').trigger('submit'); await flushPromises()
    expect(wrapper.get('[data-testid="trace-ip-error"]').text()).toContain('IP 格式不正确')
    expect(api.traceSecurityIp).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="trace-result"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="retry-trace"]').exists()).toBe(false)
  })

  it('溯源回执不可用或请求失败时显示错误条与重试，不展示任何数值', async () => {
    api.traceSecurityIp.mockResolvedValueOnce({ available: false, verified: false, kind: 'ip_trace', errors: ['宿主机溯源执行失败'] })
    const wrapper = mountPage(); await flushPromises(); await openTrace(wrapper)
    await wrapper.get('[data-testid="trace-ip-input"]').setValue('203.0.113.9')
    await wrapper.get('.trace-form').trigger('submit'); await flushPromises()
    expect(wrapper.get('#security-panel-trace').text()).toContain('溯源不可用：宿主机溯源执行失败')
    expect(wrapper.find('[data-testid="trace-result"]').exists()).toBe(false)
    await wrapper.get('[data-testid="retry-trace"]').trigger('click'); await flushPromises()
    expect(api.traceSecurityIp).toHaveBeenCalledTimes(2)
    expect(wrapper.get('[data-testid="trace-result"]').text()).toContain('高风险')
    api.traceSecurityIp.mockRejectedValueOnce(new Error('网络超时'))
    await wrapper.get('.trace-form').trigger('submit'); await flushPromises()
    expect(wrapper.get('#security-panel-trace').text()).toContain('溯源读取失败：网络超时')
    expect(wrapper.find('[data-testid="trace-result"]').exists()).toBe(false)
  })

  it('受保护来源醒目提示，归因失败只显示原因不伪造外部信息', async () => {
    api.traceSecurityIp.mockResolvedValue(traceSnapshot({ is_protected: true, attribution: { ok: false, note: '外部归因服务未配置' } }))
    const wrapper = mountPage(); await flushPromises(); await openTrace(wrapper)
    await wrapper.get('[data-testid="trace-ip-input"]').setValue('203.0.113.9')
    await wrapper.get('.trace-form').trigger('submit'); await flushPromises()
    const result = wrapper.get('[data-testid="trace-result"]')
    expect(result.get('[data-testid="trace-protected"]').text()).toContain('受保护来源，不参与自动处置')
    expect(result.get('[data-testid="trace-attribution-note"]').text()).toBe('外部被动归因，不参与评分')
    expect(result.text()).toContain('外部归因服务未配置')
    expect(result.text()).not.toContain('德国')
    expect(result.text()).not.toContain('AS64500')
  })

  it('进入溯源分区自动加载一次防御面与流量元数据，重复进入不重复请求', async () => {
    const wrapper = mountPage(); await flushPromises()
    expect(api.getDefenseSurface).not.toHaveBeenCalled()
    expect(api.getTrafficSummary).not.toHaveBeenCalled()
    await openTrace(wrapper)
    expect(api.getDefenseSurface).toHaveBeenCalledTimes(1)
    expect(api.getTrafficSummary).toHaveBeenCalledExactlyOnceWith(24)
    await wrapper.get('#security-tab-overview').trigger('click'); await flushPromises()
    await openTrace(wrapper)
    expect(api.getDefenseSurface).toHaveBeenCalledTimes(1)
    expect(api.getTrafficSummary).toHaveBeenCalledTimes(1)
  })

  it('防御面卡展示监听面、加固应用两态、ipset、封禁租约与防火墙链可读性', async () => {
    const wrapper = mountPage(); await flushPromises(); await openTrace(wrapper)
    const panel = wrapper.get('[data-testid="surface-panel"]')
    expect(panel.get('[data-testid="surface-public-listeners"]').text()).toBe('1 个')
    expect(panel.text()).toContain('nginx')
    expect(panel.text()).toContain('对外监听')
    expect(panel.text()).toContain('已安装')
    expect(panel.text()).toContain('未安装')
    expect(panel.text()).toContain('prism_block_v4')
    expect(panel.text()).toContain('实际租约 2 条')
    expect(panel.text()).toContain('2222')
    expect(panel.text()).toContain('可读')
    expect(panel.text()).toContain('不可读')
    expect(panel.text()).toContain('ip6tables')
    await panel.get('[data-testid="surface-public-only"]').setValue(true)
    const rows = panel.findAll('tbody tr')
    expect(rows).toHaveLength(1)
    expect(rows[0].text()).toContain('0.0.0.0')
  })

  it('防御面与流量回执不可用时显示错误条，重试后可恢复', async () => {
    api.getDefenseSurface.mockResolvedValueOnce({ available: false, verified: false, kind: 'surface_audit', errors: ['需要 root 权限'] })
    api.getTrafficSummary.mockResolvedValueOnce({ available: false, verified: false, kind: 'traffic_summary', errors: ['连接元数据不可用'] })
    const wrapper = mountPage(); await flushPromises(); await openTrace(wrapper)
    const surfacePanel = wrapper.get('[data-testid="surface-panel"]')
    expect(surfacePanel.text()).toContain('防御面不可用：需要 root 权限')
    expect(surfacePanel.find('[data-testid="surface-public-listeners"]').exists()).toBe(false)
    const trafficPanel = wrapper.get('[data-testid="traffic-panel"]')
    expect(trafficPanel.text()).toContain('流量元数据不可用：连接元数据不可用')
    expect(trafficPanel.find('tbody').exists()).toBe(false)
    await surfacePanel.get('[data-testid="retry-surface"]').trigger('click'); await flushPromises()
    await trafficPanel.get('[data-testid="retry-traffic"]').trigger('click'); await flushPromises()
    expect(surfacePanel.get('[data-testid="surface-public-listeners"]').text()).toBe('1 个')
    expect(trafficPanel.get('[data-testid="traffic-peer-total"]').text()).toBe('5')
  })

  it('流量面板显式声明不采集载荷，payload_captured 为 false 时不显示为已捕获', async () => {
    const wrapper = mountPage(); await flushPromises(); await openTrace(wrapper)
    const panel = wrapper.get('[data-testid="traffic-panel"]')
    const note = panel.get('[data-testid="traffic-payload-note"]')
    expect(note.text()).toContain('只采集连接元数据与日志计数，不捕获、不存储流量载荷')
    expect(note.text()).not.toContain('回执显示存在流量载荷采集')
    expect(panel.get('[data-testid="traffic-peer-total"]').text()).toBe('5')
    expect(panel.text()).toContain('当前连接数')
    expect(panel.text()).toContain('近期 SSH 失败来源')
    expect(panel.text()).toContain('203.0.113.9')
    expect(panel.text()).toContain('端口 443 × 2')
    const headers = panel.findAll('thead th').map((item) => item.text())
    expect(headers).toEqual(['对端 IP', '当前连接数', 'SSH 失败', '敏感探测', '不同目标数', '端口 / 协议', '最近时间'])
    expect(panel.findAll('tbody tr')).toHaveLength(1)
    expect(panel.get('tbody tr').text()).toContain('42')
  })

  it('载荷回执异常时改提示核验，不再宣称未采集', async () => {
    api.getTrafficSummary.mockResolvedValue({ ...trafficSnapshot(), payload_captured: true })
    const wrapper = mountPage(); await flushPromises(); await openTrace(wrapper)
    const note = wrapper.get('[data-testid="traffic-payload-note"]')
    expect(note.text()).toContain('回执显示存在流量载荷采集，请立即核验采集配置')
    expect(note.text()).not.toContain('不捕获、不存储流量载荷')
  })

  it('切换流量时间范围按小时重新查询并更新展示', async () => {
    api.getTrafficSummary.mockImplementation((hours: number) => Promise.resolve(trafficSnapshot({ window_hours: hours, peer_total: hours })))
    const wrapper = mountPage(); await flushPromises(); await openTrace(wrapper)
    await wrapper.get('[data-testid="traffic-range"]').setValue('72'); await flushPromises()
    expect(api.getTrafficSummary).toHaveBeenLastCalledWith(72)
    const panel = wrapper.get('[data-testid="traffic-panel"]')
    expect(panel.get('[data-testid="traffic-peer-total"]').text()).toBe('72')
    expect(panel.text()).toContain('72 小时')
  })
})
