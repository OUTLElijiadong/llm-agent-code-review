import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), billingFetch: vi.fn(), token: 'session-a', setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() }))
const account = vi.hoisted(() => ({ profile: { id: 1 } as { id: number } | null, token: 'session-a' }))
vi.mock('@/api/personalUsage', () => ({ getPersonalTokenUsage: mocks.fetch, getPersonalBillingUsage: mocks.billingFetch }))
vi.mock('@/utils/token', () => ({ getToken: () => mocks.token }))
vi.mock('@/stores/user', async () => {
  const { reactive } = await import('vue')
  const store = reactive(account)
  return { useUserStore: () => store }
})
vi.mock('echarts/core', () => ({
  use: vi.fn(),
  init: vi.fn(() => ({ setOption: mocks.setOption, resize: mocks.resize, dispose: mocks.dispose })),
}))
import PersonalTokenUsage from './PersonalTokenUsage.vue'
import { useUserStore } from '@/stores/user'

beforeEach(() => {
  mocks.token = 'session-a'
  account.token = 'session-a'; account.profile = { id: 1 }
  mocks.fetch.mockReset(); mocks.billingFetch.mockReset(); mocks.setOption.mockReset()
  mocks.billingFetch.mockResolvedValue({ timezone: 'UTC', items: [], scope_note: '仅统计计费记录。' })
})

describe('个人 Token 用量', () => {
  it('以可切换视图保留调用日志和计费图，费用明细默认折叠', async () => {
    mocks.fetch.mockResolvedValue({ timezone: 'UTC', days: 30, start_date: '2026-10-31', end_date: '2026-10-31',
      total_tokens: 12, input_tokens: 10, output_tokens: 2, component_unknown_calls: 0,
      known_calls: 1, unknown_calls: 0, daily: [{ date: '2026-10-31', tokens: 12, calls: 1, known_calls: 1, unknown_calls: 0 }] })
    mocks.billingFetch.mockResolvedValue({ timezone: 'UTC', scope_note: '仅统计计费记录。', items: [] })
    const wrapper = mount(PersonalTokenUsage, { attachTo: document.body })
    await flushPromises()
    const logButton = wrapper.get('[data-view="logs"]')
    const billingButton = wrapper.get('[data-view="billing"]')
    expect(logButton.attributes('aria-pressed')).toBe('true')
    expect(wrapper.get('#usage-log-view').isVisible()).toBe(true)
    expect(wrapper.get('#usage-billing-view').isVisible()).toBe(false)
    await billingButton.trigger('click')
    await nextTick()
    expect(billingButton.attributes('aria-pressed')).toBe('true')
    expect(wrapper.get('#usage-log-view').isVisible()).toBe(false)
    expect(wrapper.get('#usage-billing-view').isVisible()).toBe(true)
    expect(mocks.resize).toHaveBeenCalled()
    const details = wrapper.get('details.billing-details')
    expect(details.attributes('open')).toBeUndefined()
    expect(details.text()).toContain('独立 Search 扣费')
    expect(details.find('table').exists()).toBe(true)
    wrapper.unmount()
  })
  it('展示账号用量，缺失用量显示未知', async () => {
    mocks.fetch.mockResolvedValue({
      timezone: 'UTC', days: 30, start_date: '2026-10-30', end_date: '2026-10-31',
      total_tokens: 1234, input_tokens: 1000, output_tokens: 234, component_unknown_calls: 1,
      known_calls: 1, unknown_calls: 1,
      daily: [
        { date: '2026-10-30', tokens: 1234, calls: 1, known_calls: 1, unknown_calls: 0 },
        { date: '2026-10-31', tokens: null, calls: 1, known_calls: 0, unknown_calls: 1 },
      ],
    })
    const wrapper = mount(PersonalTokenUsage)
    await flushPromises()
    expect(mocks.fetch).toHaveBeenCalledWith(30, 'session-a')
    expect(wrapper.text()).toContain('1,234')
    expect(wrapper.text()).toContain('未知')
    expect(wrapper.text()).toContain('不代表支付账单')
    expect(mocks.setOption.mock.calls[0][0].series[0].data).toEqual([1234, null])
    wrapper.unmount()
  })

  it('请求失败时允许重试', async () => {
    mocks.fetch.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({
      timezone: 'UTC', days: 30, start_date: '2026-10-31', end_date: '2026-10-31',
      total_tokens: 0, input_tokens: 0, output_tokens: 0, component_unknown_calls: 0,
      known_calls: 0, unknown_calls: 0,
      daily: [{ date: '2026-10-31', tokens: 0, calls: 0, known_calls: 0, unknown_calls: 0 }],
    })
    const wrapper = mount(PersonalTokenUsage)
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('加载失败')
    await wrapper.get('[role="alert"] button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('没有模型调用记录')
    wrapper.unmount()
  })

  it('显示当前账号发起的企业与个人计费调用', async () => {
    mocks.fetch.mockResolvedValue({ timezone: 'UTC', days: 7, start_date: '2026-10-25', end_date: '2026-10-31',
      total_tokens: 0, input_tokens: 0, output_tokens: 0, component_unknown_calls: 0,
      known_calls: 0, unknown_calls: 0, daily: [] })
    mocks.billingFetch.mockResolvedValue({ timezone: 'UTC', scope_note: '按账号归集。', items: [
      { date: '2026-10-31', input_tokens: 100, output_tokens: 25, reported_calls: 1,
        unknown_calls: 1, pending_calls: 0, charged_milli: 500,
        enterprise_wallet_charged_milli: 300, personal_wallet_charged_milli: 200,
        server_tool_search_calls: 2, server_tool_fetch_calls: 1, server_tool_usage_reported_calls: 1,
        search_success_calls: 1, search_charged_milli: 1800, search_unknown_calls: 0, search_unknown_milli: 0,
        search_pending_calls: 1, search_pending_milli: 1800, search_not_billable_calls: 0,
        fetch_success_calls: 0, fetch_charged_milli: 0, fetch_unknown_calls: 0, fetch_unknown_milli: 0,
        fetch_pending_calls: 0, fetch_pending_milli: 0, fetch_not_billable_calls: 0 },
    ] })
    const wrapper = mount(PersonalTokenUsage)
    await flushPromises()
    expect(mocks.billingFetch).toHaveBeenCalledWith(30, 'session-a')
    expect(wrapper.text()).toContain('计费系统记录的个人与企业调用')
    expect(wrapper.text()).toContain('模型已确认扣费积分0.5')
    expect(wrapper.text()).toContain('独立 Search 扣费1.8')
    expect(wrapper.text()).toContain('0.5')
    expect(wrapper.text()).toContain('企业钱包扣费 0.3 积分')
    expect(wrapper.text()).toContain('独立 Search 成功')
    expect(wrapper.text()).toContain('独立 Search 扣费')
    expect(wrapper.text()).toContain('独立 Search 待处理（冻结）')
    expect(wrapper.text()).toContain('独立 Fetch 成功')
    expect(wrapper.text()).toContain('模型内 Search / Fetch 计数请求')
    await wrapper.get('[data-view="billing"]').trigger('click')
    expect(wrapper.get('#usage-billing-view [role="status"]').text()).toContain('未知或待结算')
    expect(wrapper.text()).toContain('2')
    expect(mocks.setOption.mock.calls[1][0].series[0].data).toEqual([125])
    wrapper.unmount()
  })

  it('待结算用量在计费趋势中显示断点，不伪装为零', async () => {
    mocks.fetch.mockResolvedValue({ timezone: 'UTC', days: 30, start_date: '2026-10-31', end_date: '2026-10-31',
      total_tokens: 0, input_tokens: 0, output_tokens: 0, component_unknown_calls: 0,
      known_calls: 0, unknown_calls: 0, daily: [] })
    mocks.billingFetch.mockResolvedValue({ timezone: 'UTC', scope_note: '按账号归集。', items: [
      { date: '2026-10-31', input_tokens: 0, output_tokens: 0, reported_calls: 0,
        unknown_calls: 0, pending_calls: 1, charged_milli: 0,
        enterprise_wallet_charged_milli: 0, personal_wallet_charged_milli: 0,
        server_tool_search_calls: 0, server_tool_fetch_calls: 0, server_tool_usage_reported_calls: 0,
        search_success_calls: 0, search_charged_milli: 0, search_unknown_calls: 0, search_unknown_milli: 0,
        search_pending_calls: 0, search_pending_milli: 0, search_not_billable_calls: 0,
        fetch_success_calls: 0, fetch_charged_milli: 0, fetch_unknown_calls: 0, fetch_unknown_milli: 0,
        fetch_pending_calls: 1, fetch_pending_milli: 1800, fetch_not_billable_calls: 0 },
    ] })

    const wrapper = mount(PersonalTokenUsage)
    await flushPromises()
    expect(mocks.setOption.mock.calls[1][0].series[0].data).toEqual([null])
    expect(wrapper.text()).toContain('待结算调用')
    wrapper.unmount()
  })

  it('认证会话失效时清除已显示的旧账号用量', async () => {
    mocks.fetch.mockResolvedValue({
      timezone: 'UTC', days: 30, start_date: '2026-10-31', end_date: '2026-10-31',
      total_tokens: 1234, input_tokens: 1000, output_tokens: 234, component_unknown_calls: 0,
      known_calls: 1, unknown_calls: 0,
      daily: [{ date: '2026-10-31', tokens: 1234, calls: 1, known_calls: 1, unknown_calls: 0 }],
    })
    const wrapper = mount(PersonalTokenUsage)
    await flushPromises()
    expect(wrapper.text()).toContain('1,234')
    window.dispatchEvent(new Event('prism:auth-expired'))
    await flushPromises()
    expect(wrapper.text()).not.toContain('1,234')
    wrapper.unmount()
  })

  it('同标签静默切换账号时立即清除旧数据并加载新账号用量', async () => {
    const response = (total: number) => ({
      timezone: 'UTC', days: 30, start_date: '2026-10-31', end_date: '2026-10-31',
      total_tokens: total, input_tokens: total, output_tokens: 0, component_unknown_calls: 0,
      known_calls: 1, unknown_calls: 0,
      daily: [{ date: '2026-10-31', tokens: total, calls: 1, known_calls: 1, unknown_calls: 0 }],
    })
    mocks.fetch.mockResolvedValueOnce(response(1234))
    const wrapper = mount(PersonalTokenUsage)
    await flushPromises()
    expect(wrapper.text()).toContain('1,234')

    let resolveNew!: (value: ReturnType<typeof response>) => void
    mocks.fetch.mockImplementationOnce(() => new Promise(resolve => { resolveNew = resolve }))
    mocks.token = 'session-b'
    useUserStore().token = 'session-b'
    useUserStore().profile!.id = 2
    await nextTick()
    expect(wrapper.text()).not.toContain('1,234')
    await flushPromises()

    expect(mocks.fetch).toHaveBeenLastCalledWith(30, 'session-b')
    resolveNew(response(5678))
    await flushPromises()
    expect(wrapper.text()).not.toContain('1,234')
    expect(wrapper.text()).toContain('5,678')
    expect(mocks.fetch).toHaveBeenLastCalledWith(30, 'session-b')
    wrapper.unmount()
  })

  it('丢弃账号切换前迟到的用量响应', async () => {
    const response = (total: number) => ({
      timezone: 'UTC', days: 30, start_date: '2026-10-31', end_date: '2026-10-31',
      total_tokens: total, input_tokens: total, output_tokens: 0, component_unknown_calls: 0,
      known_calls: 1, unknown_calls: 0,
      daily: [{ date: '2026-10-31', tokens: total, calls: 1, known_calls: 1, unknown_calls: 0 }],
    })
    let resolveOld!: (value: ReturnType<typeof response>) => void
    mocks.fetch.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
    const wrapper = mount(PersonalTokenUsage)
    await flushPromises()
    expect(mocks.fetch).toHaveBeenCalledWith(30, 'session-a')

    mocks.fetch.mockResolvedValueOnce(response(5678))
    mocks.token = 'session-b'
    useUserStore().token = 'session-b'
    useUserStore().profile!.id = 2
    await flushPromises()
    expect(wrapper.text()).toContain('5,678')

    resolveOld(response(1234))
    await flushPromises()
    expect(wrapper.text()).toContain('5,678')
    expect(wrapper.text()).not.toContain('1,234')
    wrapper.unmount()
  })
})
