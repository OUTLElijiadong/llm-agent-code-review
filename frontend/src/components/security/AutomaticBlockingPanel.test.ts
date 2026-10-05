import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import AutomaticBlockingPanel from './AutomaticBlockingPanel.vue'

const api = vi.hoisted(() => ({
  getAutomaticBlocking: vi.fn(),
  updateAutomaticBlocking: vi.fn(),
  releaseAutomaticBlock: vi.fn(),
  prompt: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
}))
vi.mock('@/api/adminSecurityCenter', () => ({
  getAutomaticBlocking: api.getAutomaticBlocking,
  updateAutomaticBlocking: api.updateAutomaticBlocking,
  releaseAutomaticBlock: api.releaseAutomaticBlock,
}))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { success: api.success, error: api.error, warning: api.warning } }))
vi.mock('element-plus/es/components/message-box/index', () => ({ ElMessageBox: { prompt: api.prompt } }))

function snapshot() {
  return {
    available: true, verified: true, enabled: false, backend: 'ipset' as const,
    policy: { enabled: false, ai_anomaly_enabled: false, duration_seconds: 900, window_seconds: 300, ssh_threshold: 20, web_threshold: 30, allowlist_cidrs: ['198.51.100.0/24'], activated_at: null },
    protected_sources: [{ cidr: '127.0.0.0/8', reason: '本机来源' }],
    active_blocks: [], recent_blocks: [], last_evaluated_at: null, errors: [],
  }
}

function entry(status = 'active', ip = '203.0.113.9') {
  return { id: `block-${ip}`, ip, rule: 'ssh_failed_password', evidence_count: 20, scope: 'INPUT+DOCKER-USER', status, started_at: '2026-10-05T10:00:00Z', expires_at: '2026-10-05T10:15:00Z', released_at: null, reason: '短窗口重复认证失败' }
}

const mounted: ReturnType<typeof mount>[] = []
function mountPanel() {
  const wrapper = mount(AutomaticBlockingPanel)
  mounted.push(wrapper)
  return wrapper
}
function button(wrapper: ReturnType<typeof mount>, text: string) {
  return wrapper.findAll('button').find((item) => item.text() === text)!
}

describe('AutomaticBlockingPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getAutomaticBlocking.mockResolvedValue(snapshot())
    api.updateAutomaticBlocking.mockResolvedValue(snapshot())
    api.releaseAutomaticBlock.mockResolvedValue({})
  })
  afterEach(() => {
    mounted.splice(0).forEach((wrapper) => wrapper.unmount())
    vi.useRealTimers()
  })

  it('shows confirmed policy and protected sources with explicit rule boundaries', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('自动封禁已关闭')
    expect(wrapper.text()).toContain('最长 15 分钟')
    expect(wrapper.text()).toContain('普通 403')
    expect(wrapper.text()).toContain('127.0.0.0/8')
    expect(wrapper.text()).toContain('本机来源')
    expect(wrapper.text()).toContain('暂无匹配的封禁记录')
    expect(wrapper.get('input[type="checkbox"]').element).not.toHaveProperty('checked', true)
  })

  it('uses an unknown state on load failure and prevents saving an unknown policy', async () => {
    api.getAutomaticBlocking.mockRejectedValue(new Error('读取超时'))
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('状态未知')
    expect(wrapper.text()).toContain('读取超时')
    expect(wrapper.text()).not.toContain('当前确认生效 0')
    expect(button(wrapper, '保存自动封禁策略').attributes('disabled')).toBeDefined()
  })

  it('requires an available executor to enable blocking but allows turning it off', async () => {
    api.getAutomaticBlocking.mockResolvedValue({ ...snapshot(), available: false, enabled: true, policy: { ...snapshot().policy, enabled: true }, errors: ['ipset 不可用'] })
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('执行器不可用')
    expect(wrapper.text()).toContain('ipset 不可用')
    await wrapper.get('input[type="checkbox"]').setValue(false)
    api.getAutomaticBlocking.mockResolvedValue(snapshot())
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(api.updateAutomaticBlocking).toHaveBeenCalledWith(expect.objectContaining({ enabled: false }))
  })

  it('never claims active blocking from an unverified executor receipt', async () => {
    api.getAutomaticBlocking.mockResolvedValue({ ...snapshot(), verified: false, enabled: true, policy: { ...snapshot().policy, enabled: true }, active_blocks: [entry()] })
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('自动封禁执行状态待核验')
    expect(wrapper.text()).toContain('生效数量待核验')
    expect(wrapper.text()).toContain('状态待核验')
    expect(wrapper.text()).not.toContain('已生效（临时）')
    expect(wrapper.find('.blocking-state.enabled').exists()).toBe(false)
    expect(button(wrapper, '手动解封').attributes('disabled')).toBeDefined()
    expect(button(wrapper, '保存自动封禁策略').attributes('disabled')).toBeDefined()
  })

  it('retains draft on save failure and does not claim the changed policy became effective', async () => {
    api.updateAutomaticBlocking.mockRejectedValue(new Error('策略未写入'))
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('input[type="checkbox"]').setValue(true)
    await wrapper.get('textarea').setValue('198.51.100.0/24\n2001:db8::/64')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(api.updateAutomaticBlocking).toHaveBeenCalledWith({ enabled: true, ai_anomaly_enabled: false, duration_seconds: 900, window_seconds: 300, ssh_threshold: 20, web_threshold: 30, allowlist_cidrs: ['198.51.100.0/24', '2001:db8::/64'] })
    expect(wrapper.text()).toContain('自动封禁已关闭')
    expect(wrapper.text()).toContain('策略未写入')
    expect((wrapper.get('input[type="checkbox"]').element as HTMLInputElement).checked).toBe(true)
    expect(api.success).not.toHaveBeenCalled()
  })

  it('rereads server policy after saving and reports unconfirmed reads accurately', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('input[type="checkbox"]').setValue(true)
    api.getAutomaticBlocking.mockRejectedValueOnce(new Error('回读失败'))
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(api.getAutomaticBlocking).toHaveBeenCalledTimes(2)
    expect(api.success).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('状态未知')
    expect(api.warning).toHaveBeenCalledWith(expect.stringContaining('重新读取失败'))
    expect(button(wrapper, '保存自动封禁策略').attributes('disabled')).toBeDefined()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(api.updateAutomaticBlocking).toHaveBeenCalledTimes(1)
    await button(wrapper, '刷新封禁状态').trigger('click')
    await flushPromises()
    expect(button(wrapper, '保存自动封禁策略').attributes('disabled')).toBeUndefined()
  })

  it('only acknowledges a policy after the matching verified server state is reread', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const enabled = { ...snapshot(), enabled: true, policy: { ...snapshot().policy, enabled: true } }
    api.getAutomaticBlocking.mockResolvedValueOnce(enabled)
    await wrapper.get('input[type="checkbox"]').setValue(true)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('自动封禁已启用')
    expect(api.getAutomaticBlocking).toHaveBeenCalledTimes(2)
    expect(api.success).toHaveBeenCalledWith(expect.stringContaining('已回读确认'))
  })

  it('keeps AI decisions within the explicit protected candidate and short-lease policy', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.get('.ai-field input').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('每天最多 24 次模型研判')
    expect(wrapper.text()).toContain('最多 2 分钟')
    expect(wrapper.text()).toContain('SSH 认证失败至少 10 次')
    expect(wrapper.text()).toContain('3 个敏感目标')
    expect(wrapper.text()).toContain('不能任意封 IP、网段或永久封禁')
    await wrapper.get('.enable-field input').setValue(true)
    await wrapper.get('.ai-field input').setValue(true)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(api.updateAutomaticBlocking).toHaveBeenCalledWith(expect.objectContaining({ enabled: true, ai_anomaly_enabled: true }))
  })

  it('shows the actual verified IP-family support and identifies AI anomaly records', async () => {
    api.getAutomaticBlocking.mockResolvedValue({ ...snapshot(), family_support: { ipv4: true, ipv6: false }, active_blocks: [{ ...entry(), source: 'xiaoling_anomaly' }, { ...entry('active', '203.0.113.10'), rule: 'web_sensitive_probe', source: 'xiaoling_anomaly' }] })
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('IPv4：已核验支持')
    expect(wrapper.text()).toContain('IPv6：不支持')
    expect(wrapper.text()).toContain('小菱异常研判 · SSH')
    expect(wrapper.text()).toContain('小菱异常研判 · Web')
  })

  it('does not turn an unverified mutation receipt into a successful save toast', async () => {
    api.updateAutomaticBlocking.mockResolvedValue({ ...snapshot(), verified: false, available: false, outcome_unknown: true })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(api.getAutomaticBlocking).toHaveBeenCalledTimes(2)
    expect(api.success).not.toHaveBeenCalled()
    expect(api.warning).toHaveBeenCalledWith(expect.stringContaining('回执未确认'))
  })

  it('rechecks an expiring lease once without claiming the local clock proves release', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-05T10:00:00Z'))
    const expiresSoon = { ...entry(), expires_at: '2026-10-05T10:01:00Z' }
    api.getAutomaticBlocking.mockResolvedValue({ ...snapshot(), active_blocks: [expiresSoon] })
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('当前确认生效 1 个来源')
    await vi.advanceTimersByTimeAsync(60_200)
    await flushPromises()
    expect(api.getAutomaticBlocking).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('到期状态待核验')
    expect(wrapper.text()).not.toContain('已解封')
    expect(wrapper.text()).not.toContain('已生效（临时）')
    await vi.advanceTimersByTimeAsync(5 * 60_000)
    expect(api.getAutomaticBlocking).toHaveBeenCalledTimes(2)
  })

  it('rejects excessive allowlist input before issuing a policy mutation', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.get('textarea').setValue(Array.from({ length: 33 }, (_, index) => `198.51.100.${index + 1}/32`).join('\n'))
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(api.updateAutomaticBlocking).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('白名单最多保存 32 条')
  })

  it('keeps only confirmed active records actionable and labels other states separately', async () => {
    api.getAutomaticBlocking.mockResolvedValue({ ...snapshot(), active_blocks: [entry()], recent_blocks: ['expired', 'released', 'failed', 'unknown'].map((status, index) => entry(status, `203.0.113.${index + 10}`)) })
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('当前确认生效 1')
    for (const label of ['已生效（临时）', '已到期', '已解封', '执行失败', '状态待核验']) expect(wrapper.text()).toContain(label)
    expect(wrapper.findAll('button').filter((item) => item.text() === '手动解封')).toHaveLength(1)
  })

  it('does not send a release on cancel and preserves a block when release fails', async () => {
    api.getAutomaticBlocking.mockResolvedValue({ ...snapshot(), active_blocks: [entry()] })
    const wrapper = mountPanel()
    await flushPromises()
    api.prompt.mockRejectedValueOnce('cancel')
    await button(wrapper, '手动解封').trigger('click')
    await flushPromises()
    expect(api.releaseAutomaticBlock).not.toHaveBeenCalled()
    api.prompt.mockResolvedValueOnce({ value: '管理员确认合法来源' })
    api.releaseAutomaticBlock.mockRejectedValueOnce(new Error('执行器离线'))
    await button(wrapper, '手动解封').trigger('click')
    await flushPromises()
    expect(api.releaseAutomaticBlock).toHaveBeenCalledWith({ ip: '203.0.113.9', reason: '管理员确认合法来源' })
    expect(wrapper.text()).toContain('已生效（临时）')
    expect(api.success).not.toHaveBeenCalled()
  })

  it('confirms a release by rereading and never claims release success if it remains active', async () => {
    api.getAutomaticBlocking.mockResolvedValue({ ...snapshot(), active_blocks: [entry()] })
    api.prompt.mockResolvedValue({ value: '核验后解除' })
    const wrapper = mountPanel()
    await flushPromises()
    await button(wrapper, '手动解封').trigger('click')
    await flushPromises()
    expect(api.getAutomaticBlocking).toHaveBeenCalledTimes(2)
    expect(api.success).not.toHaveBeenCalled()
    expect(api.warning).toHaveBeenCalledWith(expect.stringContaining('仍显示生效'))
    api.getAutomaticBlocking.mockResolvedValue({ ...snapshot(), recent_blocks: [entry('released')] })
    await button(wrapper, '手动解封').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('已解封')
    expect(api.success).toHaveBeenCalledWith(expect.stringContaining('已核验解封'))
  })

  it('ignores an older refresh response that returns after the current request', async () => {
    let first: (value: ReturnType<typeof snapshot>) => void = () => undefined
    api.getAutomaticBlocking.mockImplementationOnce(() => new Promise((resolve) => { first = resolve }))
    const wrapper = mountPanel()
    const newer = { ...snapshot(), enabled: true, policy: { ...snapshot().policy, enabled: true } }
    api.getAutomaticBlocking.mockResolvedValueOnce(newer)
    await button(wrapper, '刷新封禁状态').trigger('click')
    await flushPromises()
    first(snapshot())
    await flushPromises()
    expect(wrapper.text()).toContain('自动封禁已启用')
    expect(wrapper.text()).not.toContain('自动封禁已关闭')
  })
})
