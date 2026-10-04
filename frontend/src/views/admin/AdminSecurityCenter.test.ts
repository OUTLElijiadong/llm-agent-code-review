import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import AdminSecurityCenter from './AdminSecurityCenter.vue'
import securityCenterSource from './AdminSecurityCenter.vue?raw'

const api = vi.hoisted(() => ({
  getSecurityCenterOverview: vi.fn(),
  getSecurityCenterEvents: vi.fn(),
  runSecurityMonitor: vi.fn(),
  updateSecurityMonitorPolicy: vi.fn(),
  getSystemStatus: vi.fn(),
  resolveAlert: vi.fn(),
  messageSuccess: vi.fn(),
  messageWarning: vi.fn(),
  messageError: vi.fn(),
}))
const mountedPages: ReturnType<typeof mount>[] = []

function mountPage() {
  const wrapper = mount(AdminSecurityCenter)
  mountedPages.push(wrapper)
  return wrapper
}

vi.mock('@/api/adminSecurityCenter', () => ({
  getSecurityCenterOverview: api.getSecurityCenterOverview,
  getSecurityCenterEvents: api.getSecurityCenterEvents,
  runSecurityMonitor: api.runSecurityMonitor,
  updateSecurityMonitorPolicy: api.updateSecurityMonitorPolicy,
}))
vi.mock('@/api/adminOverview', () => ({ getSystemStatus: api.getSystemStatus }))
vi.mock('@/api/adminGovernance', () => ({ resolveAlert: api.resolveAlert }))
vi.mock('element-plus/es/components/message/index', () => ({
  ElMessage: { success: api.messageSuccess, warning: api.messageWarning, error: api.messageError },
}))
vi.mock('element-plus/es/components/message-box/index', () => ({
  ElMessageBox: { prompt: vi.fn() },
}))

function overview() {
  return {
    generated_at: '2026-10-05T12:00:00Z',
    monitoring: {
      enabled: true,
      schedule: '*/5 * * * *',
      schedule_label: '每 5 分钟',
      interval_minutes: 5,
      last_run: { status: 'success', started_at: '2026-10-05T11:55:00Z', finished_at: '2026-10-05T11:55:12Z', completed_sources: 4, failed_sources: 0, degraded: false },
      sources: [{ code: 'nginx_attack_events', label: '读取 Nginx 请求日志', status: 'success' as const }],
    },
    policy: {
      ssh_failed_threshold: 20,
      ssh_window_hours: 1,
      nginx_failure_threshold: 20,
      nginx_window_hours: 1,
      popup_min_severity: 'warning' as const,
      monitoring_mode: 'monitor_only' as const,
      automatic_blocking_enabled: false as const,
      counterattack_enabled: false as const,
      source: 'environment',
      revision: 0,
      updated_at: null,
      updated_by: null,
      baseline: { ssh_failed_threshold: 20, ssh_window_hours: 1, nginx_failure_threshold: 20, nginx_window_hours: 1, popup_min_severity: 'warning' as const },
    },
    open_alerts_24h: 1,
    open_alerts_total: 1,
  }
}

function eventPage(title = 'Nginx 重复 HTTP 异常') {
  return {
    items: [{
      id: 'alert:9', alert_id: 9, recorded_at: '2026-10-05T11:50:00Z', event_type: 'alert', layer: '规则告警',
      severity: 'warning', status: 'open', actor: '安全监控规则', title,
      summary: '规则命中并生成告警；这不等同于攻击成功或已被拦截。',
      evidence_summary: { ip: '203.0.113.9', failure_count: 20, threshold: 20 }, resolution: null,
    }],
    total: 1, page: 1, page_size: 20, pages: 1, truncated: false, hours: 24,
  }
}

describe('AdminSecurityCenter', () => {
  afterEach(() => {
    mountedPages.splice(0).forEach((wrapper) => wrapper.unmount())
    vi.useRealTimers()
  })

  beforeEach(() => {
    vi.clearAllMocks()
    api.getSecurityCenterOverview.mockResolvedValue(overview())
    api.getSecurityCenterEvents.mockResolvedValue(eventPage())
    api.getSystemStatus.mockResolvedValue({
      available: true, collected_at: '2026-10-05T12:00:00Z', process_uptime_seconds: 10,
      cpu_percent: 4, memory_percent: 35, disk_percent: 48, disk_used_gb: 48, disk_total_gb: 100,
      uptime_seconds: 86400,
    })
  })

  it('displays passive monitoring, persisted event evidence, and does not claim the source was blocked', async () => {
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('小菱安全中心')
    expect(wrapper.text()).toContain('仅监控与告警')
    expect(wrapper.text()).toContain('自动封禁与反击当前未启用')
    expect(wrapper.text()).toContain('Nginx 重复 HTTP 异常')
    expect(wrapper.text()).toContain('203.0.113.9')
    expect(wrapper.text()).toContain('不等同于攻击成功或已被拦截')
    expect(wrapper.text()).toContain('SSH/Nginx 规则告警与采集回执')
    expect(wrapper.text()).toContain('这不代表完整的 SSH/Nginx 原始日志')
    expect(wrapper.text()).toContain('没有记录不能证明该请求未发生')
    expect(wrapper.text()).not.toContain('已拦截来源')
    expect(api.getSecurityCenterEvents).toHaveBeenCalledWith(24, 1, 20)
  })

  it('shows an unknown state and an error when monitoring data cannot be loaded', async () => {
    api.getSecurityCenterOverview.mockRejectedValue(new Error('访问被拒绝'))
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('安全状态读取失败：访问被拒绝')
    expect(wrapper.text()).toContain('状态未知')
    expect(wrapper.text()).not.toContain('监控运行中')
  })

  it('does not present a stale running inspection as currently running', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-05T12:00:00Z'))
    api.getSecurityCenterOverview.mockResolvedValue({
      ...overview(),
      monitoring: {
        ...overview().monitoring,
        last_run: {
          status: 'running', started_at: '2026-10-05T11:00:00Z', finished_at: null,
          completed_sources: 0, failed_sources: 0, degraded: false,
        },
      },
    })
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('巡检超过预期')
    expect(wrapper.text()).not.toContain('巡检进行中')
  })

  it('updates the inspection state to stale while the page remains open', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-05T12:00:00Z'))
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('近期巡检正常')
    await vi.advanceTimersByTimeAsync(16 * 60_000)

    expect(wrapper.text()).toContain('巡检记录已过期')
    expect(wrapper.text()).toContain('最近巡检已过期')
  })

  it('labels a success record without a finish time as incomplete', async () => {
    api.getSecurityCenterOverview.mockResolvedValue({
      ...overview(),
      monitoring: {
        ...overview().monitoring,
        last_run: { ...overview().monitoring.last_run, finished_at: null },
      },
    })
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.text()).toContain('巡检记录不完整')
    expect(wrapper.text()).not.toContain('最近巡检完成')
  })

  it('clears old event rows when a new time range fails to load', async () => {
    api.getSecurityCenterEvents.mockResolvedValueOnce(eventPage())
    const wrapper = mountPage()
    await flushPromises()
    api.getSecurityCenterEvents.mockRejectedValueOnce(new Error('网络超时'))

    await wrapper.get('select').setValue(168)
    await flushPromises()

    expect(wrapper.text()).toContain('事件记录读取失败：网络超时')
    expect(wrapper.text()).not.toContain('Nginx 重复 HTTP 异常')
    expect(api.getSecurityCenterEvents).toHaveBeenLastCalledWith(168, 1, 20)
  })

  it('ignores an older event query that resolves after the latest filter query', async () => {
    let resolveInitial: (value: ReturnType<typeof eventPage>) => void = () => undefined
    let resolveLatest: (value: ReturnType<typeof eventPage>) => void = () => undefined
    api.getSecurityCenterEvents
      .mockImplementationOnce(() => new Promise((resolve) => { resolveInitial = resolve }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveLatest = resolve }))
    const wrapper = mountPage()
    await wrapper.get('select').setValue(168)
    resolveLatest(eventPage('最新范围事件'))
    await flushPromises()
    resolveInitial(eventPage('过期范围事件'))
    await flushPromises()

    expect(wrapper.text()).toContain('最新范围事件')
    expect(wrapper.text()).not.toContain('过期范围事件')
  })

  it('manual inspection refreshes persisted records, and policy updates are sent through the restricted API', async () => {
    api.runSecurityMonitor.mockResolvedValue({ success: true, created_alerts: [], errors: [] })
    api.updateSecurityMonitorPolicy.mockResolvedValue({ ...overview().policy, revision: 1, source: 'database' })
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.get('button.button-primary').trigger('click')
    await flushPromises()
    expect(api.runSecurityMonitor).toHaveBeenCalledOnce()
    expect(api.getSecurityCenterOverview).toHaveBeenCalledTimes(2)

    // jsdom 的 click 不会自动派发原生 form submit；直接驱动真实提交事件。
    await wrapper.get('.policy-form').trigger('submit')
    await flushPromises()
    expect(api.updateSecurityMonitorPolicy).toHaveBeenCalledWith({
      ssh_failed_threshold: 20,
      ssh_window_hours: 1,
      nginx_failure_threshold: 20,
      nginx_window_hours: 1,
    })
  })

  it('keeps tablet and phone layouts single-column without hard viewport widths', () => {
    expect(securityCenterSource).toContain('@media (max-width: 920px)')
    expect(securityCenterSource).toContain('.content-grid { grid-template-columns: 1fr; }')
    expect(securityCenterSource).toContain('@media (max-width: 640px)')
    expect(securityCenterSource).toContain('.runtime-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }')
    expect(securityCenterSource).toContain('.event-status { grid-column: 3;')
  })

  it('stacks security evidence scope disclosures into a readable mobile column', () => {
    expect(securityCenterSource).toMatch(/\.scope-note\s*\{[^}]*display:\s*grid/)
    expect(securityCenterSource).toMatch(/\.scope-note\s+p\s*\{[^}]*grid-column:\s*2/)
    expect(securityCenterSource).toMatch(/\.scope-note\s+svg\s*\{[^}]*grid-row:\s*1\s*\/\s*span\s*2/)
  })
})
