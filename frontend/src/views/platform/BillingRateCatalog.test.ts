import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  pricing: vi.fn(), list: vi.fn(), create: vi.fn(), approve: vi.fn(), reject: vi.fn(), cancel: vi.fn(), cancelRequest: vi.fn(),
}))
const account = vi.hoisted(() => ({
  profile: { id: 1, username: 'pricing-admin', role: 'super_admin', status: 1 } as
    { id: number; username: string; role: string; status: number } | null,
  token: 'catalog-session',
}))
vi.mock('@/api/billing', () => ({
  getBillingPricing: api.pricing,
  listBillingRateCatalogs: api.list,
  createBillingRateCatalog: api.create,
  approveBillingRateCatalog: api.approve,
  rejectBillingRateCatalog: api.reject,
  cancelBillingRateCatalog: api.cancel,
  proposeBillingRateCancellation: api.cancelRequest,
}))
vi.mock('@/utils/token', () => ({ getToken: () => account.token }))
vi.mock('@/stores/user', () => ({ useUserStore: () => account }))

import BillingRateCatalog from './BillingRateCatalog.vue'

const pricing = {
  currency: 'CNY' as const, pricing_version: 'a'.repeat(64),
  pricing_effective_at: '2026-11-07T00:00:00Z', next_pricing_effective_at: null,
  billing_unit: { unit: 'milli_credit' as const, milli_per_credit: 1000,
    rate_unit: 'milli_credit_per_token' as const, rounding: 'ceil_per_model_request' as const },
  credit_pack: { credits: 1000, price_fen: 400 }, reviewer_month: { price_fen: 4900, credits: 5000 },
  enterprise_seat_month: { price_fen: 6900, credits: 5000 },
  model_milli_per_token: {
    'deepseek-flash': { input: 1, output: 4 }, 'deepseek-v4-flash': { input: 1, output: 4 },
    'deepseek-v4-flash-vision-exp': { input: 1, output: 4 },
  }, tool_milli_credits_per_success: { search: null, fetch: null },
  billable_models: ['deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp'],
  channels: { alipay: false, wechat: false }, sales_enabled: false,
}
const item = (model_name: string) => ({
  model_name, input_rate_numerator: 1, input_rate_denominator: 1,
  output_rate_numerator: 4, output_rate_denominator: 1,
  provider_input_usd_micros_per_million: 300000,
  provider_output_usd_micros_per_million: 1200000,
  provider_input_cny_micros_per_million: null,
  provider_output_cny_micros_per_million: null,
})
const baseCatalog = {
  id: 1, version_key: 'a'.repeat(64), status: 'approved' as const,
  effective_at: '2026-11-07T00:00:00Z', source_url: 'https://api-docs.deepseek.com/quick_start/pricing/',
  model_cny_source_url: null, model_cny_source_checked_at: null,
  source_checked_at: '2026-11-07T05:30:00Z', fx_source_url: 'https://www.boc.cn/sourcedb/whpj/index.html',
  fx_source_checked_at: '2026-11-07T05:30:00Z', source_sha256: 'b'.repeat(64), fx_cny_per_usd: 6.7168,
  credit_pack_price_fen: 400, credit_pack_credits: 1000, change_note: '当前基线费率。',
  tool_cny_source_url: null, tool_cny_source_checked_at: null,
  tool_usd_source_url: null, tool_usd_source_checked_at: null,
  tool_search_cost_cny_micros_per_success: 0, tool_search_price_milli_per_success: 0,
  tool_fetch_cost_cny_micros_per_success: 0, tool_fetch_price_milli_per_success: 0,
  created_by: null, created_at: '2026-11-07T05:30:00Z', approved_by: null, approved_at: null,
  rejected_by: null, rejected_at: null, canceled_by: null, canceled_at: null, transition_note: null,
  minimum_direct_margin_bps: 3000,
  margin_bps: {
    'deepseek-flash': { input_bps: 4962, output_bps: 4962 },
    'deepseek-v4-flash': { input_bps: 4962, output_bps: 4962 },
    'deepseek-v4-flash-vision-exp': { input_bps: 4962, output_bps: 4962 },
  },
  tool_margin_bps: { search: null, fetch: null },
  tool_supplier_price_diff: { changes: [], increases: [], decreases: [] },
  supplier_price_diff: { changes: [], increases: [], decreases: [],
    tool_changes: [], tool_increases: [], tool_decreases: [] },
  items: ['deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp'].map(item),
  events: [{ actor_id: null, action: 'created' as const, note: '迁移初始化。', created_at: '2026-11-07T05:30:00Z' }],
}

beforeEach(() => {
  Object.values(api).forEach(fn => fn.mockReset())
  account.profile = { id: 1, username: 'pricing-admin', role: 'super_admin', status: 1 }
  account.token = 'catalog-session'
  api.pricing.mockResolvedValue(pricing)
  api.list.mockResolvedValue({ items: [baseCatalog] })
  api.create.mockResolvedValue({ ...baseCatalog, id: 2, status: 'draft', created_by: 1 })
  api.approve.mockResolvedValue({ ...baseCatalog, id: 2, status: 'approved', created_by: 9, approved_by: 1 })
  api.reject.mockResolvedValue({ ...baseCatalog, id: 2, status: 'rejected', created_by: 9, rejected_by: 1 })
  api.cancelRequest.mockResolvedValue({ id: 42, catalog_id: 2, status: 'pending' })
  api.cancel.mockResolvedValue({ ...baseCatalog, id: 2, status: 'canceled', created_by: 9, approved_by: 8, canceled_by: 1 })
})

describe('平台费率目录工作台', () => {
  it('申请取消计划费率，等待另一名复核人决定', async () => {
    api.list.mockResolvedValue({ items: [{ ...baseCatalog, id: 2, effective_at: '2099-01-01T00:00:00Z', created_by: 1 }] })
    const wrapper = mount(BillingRateCatalog); await flushPromises()
    await wrapper.get('[data-cancel-note="2"]').setValue('需重新核对来源')
    await wrapper.get('[data-cancel-request="2"]').trigger('click'); await flushPromises()
    expect(api.cancelRequest).toHaveBeenCalledWith(2, '需重新核对来源', 'catalog-session')
    expect(api.cancel).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('申请已提交，等待另一位费率复核人决定')
    wrapper.unmount()
  })

  it('展示报价版本、毛利边界和供应商来源事实摘要', async () => {
    const wrapper = mount(BillingRateCatalog)
    await flushPromises()
    expect(api.pricing).toHaveBeenCalledOnce()
    expect(api.list).toHaveBeenCalledWith('catalog-session')
    expect(wrapper.text()).toContain('开售锁仍关闭')
    expect(wrapper.text()).toContain('30% 是模型和按次工具的直采最低毛利线')
    expect(wrapper.text()).toContain('300000 表示每百万 Token 0.30 美元')
    expect(wrapper.text()).toContain('bbbbbbbbbbbb')
    expect(wrapper.text()).toContain('人民币价格未核验，无法确认双币毛利')
    expect(wrapper.text()).toContain('中国银行外汇牌价页面')
    expect(wrapper.text()).toContain('汇率来源')
    expect(wrapper.text()).toContain('企业小菱 Search')
    expect(wrapper.text()).toContain('未开通')
    wrapper.unmount()
  })

  it('历史目录不能自动伪造人民币核验来源和成本', async () => {
    const wrapper = mount(BillingRateCatalog)
    await flushPromises()
    expect(wrapper.text()).toContain('模型人民币价格来源未核验')
    expect((wrapper.get('input[aria-label="模型人民币价格核验时间"]').element as HTMLInputElement).value).toBe('')
    const input = wrapper.get('input[aria-label="deepseek-flash 供应商输入价格，人民币微元每百万 Token"]')
    expect((input.element as HTMLInputElement).value).toBe('')
    await wrapper.get('textarea').setValue('新目录核验')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(api.create).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('请填写有效的生效时间、模型人民币与美元价格核验时间')
    wrapper.unmount()
  })

  it('已核验双币目录显示人民币成本与双币毛利', async () => {
    api.list.mockResolvedValue({ items: [{ ...baseCatalog,
      model_cny_source_url: 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
      model_cny_source_checked_at: '2026-11-07T05:30:00Z',
      items: baseCatalog.items.map(row => ({ ...row, provider_input_cny_micros_per_million: 2000000,
        provider_output_cny_micros_per_million: 8000000 })),
    }] })
    const wrapper = mount(BillingRateCatalog); await flushPromises()
    expect(wrapper.text()).toContain('人民币输入 ¥2.00 · 输出 ¥8.00 / 百万 Token')
    expect(wrapper.text()).toContain('输入 49.62% · 输出 49.62%')
    wrapper.unmount()
  })

  it('不能让草稿创建人自批，批准动作会携带当前会话 token', async () => {
    api.list.mockResolvedValue({ items: [{ ...baseCatalog, id: 2, status: 'draft', created_by: 1 }] })
    const wrapper = mount(BillingRateCatalog)
    await flushPromises()
    expect(wrapper.text()).toContain('你是草稿创建人，不能审批本人版本')
    expect(wrapper.text()).not.toContain('批准计划费率')
    wrapper.unmount()

    api.list.mockResolvedValue({ items: [{ ...baseCatalog, id: 2, status: 'draft', created_by: 9 }] })
    const reviewer = mount(BillingRateCatalog)
    await flushPromises()
    const approve = reviewer.findAll('button').find(button => button.text() === '批准计划费率')!
    await approve.trigger('click')
    await flushPromises()
    expect(api.approve).toHaveBeenCalledWith(2, 'catalog-session')
    expect(reviewer.text()).toContain('费率版本已批准')
    reviewer.unmount()
  })

  it('创建草稿时将本地时间转换为 UTC，并不接受用户输入价格事实摘要', async () => {
    const draft = { ...baseCatalog, id: 2, status: 'draft' as const, created_by: 1,
      events: [...baseCatalog.events, { actor_id: 1, action: 'created' as const,
        note: '已核对官方价目，更新成本和收费费率。', created_at: '2026-11-07T06:00:00Z' }] }
    api.create.mockImplementation(async () => {
      api.list.mockResolvedValue({ items: [draft, baseCatalog] })
      return draft
    })
    const wrapper = mount(BillingRateCatalog)
    await flushPromises()
    expect((wrapper.get('input[aria-label="模型人民币价格核验时间"]').element as HTMLInputElement).value).toBe('')
    await wrapper.get('input[aria-label="模型人民币价格核验时间"]').setValue('2026-11-07T08:00')
    for (const model of pricing.billable_models) {
      await wrapper.get(`input[aria-label="${model} 供应商输入价格，人民币微元每百万 Token"]`).setValue('2000000')
      await wrapper.get(`input[aria-label="${model} 供应商输出价格，人民币微元每百万 Token"]`).setValue('8000000')
    }
    await wrapper.get('textarea').setValue('已核对官方价目，更新成本和收费费率。')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(api.create).toHaveBeenCalledOnce()
    const [payload, token] = api.create.mock.calls[0]
    expect(payload.effective_at).toMatch(/Z$/)
    expect(payload.source_checked_at).toMatch(/Z$/)
    expect(payload.model_cny_source_url).toBe('https://api-docs.deepseek.com/zh-cn/quick_start/pricing/')
    expect(payload.model_cny_source_checked_at).toMatch(/Z$/)
    expect(payload.items[0].provider_input_cny_micros_per_million).toBe(2000000)
    expect(payload.items[0].provider_output_cny_micros_per_million).toBe(8000000)
    expect(payload.fx_source_checked_at).toMatch(/Z$/)
    expect(payload.fx_source_url).toBe('https://www.boc.cn/sourcedb/whpj/index.html')
    expect(payload).not.toHaveProperty('source_sha256')
    expect(payload.items).toHaveLength(3)
    expect(token).toBe('catalog-session')
    expect(wrapper.text()).toContain('需要另一位获授权的费率复核人审批')
    wrapper.unmount()
  })

  it('可以在草稿中录入独立的 Search/Fetch 价格来源、成本和客户费率', async () => {
    const wrapper = mount(BillingRateCatalog)
    await flushPromises()
    await wrapper.get('input[aria-label="模型人民币价格核验时间"]').setValue('2026-11-07T08:00')
    for (const model of pricing.billable_models) {
      await wrapper.get(`input[aria-label="${model} 供应商输入价格，人民币微元每百万 Token"]`).setValue('2000000')
      await wrapper.get(`input[aria-label="${model} 供应商输出价格，人民币微元每百万 Token"]`).setValue('8000000')
    }
    await wrapper.get('textarea').setValue('核对 Search/Fetch 官方成功请求价格。')
    await wrapper.get('input[aria-label="Search 供应商成本，人民币微元每成功请求"]').setValue('5038')
    await wrapper.get('input[aria-label="Search 用户费率，毫积分每成功请求"]').setValue('1800')
    await wrapper.get('input[aria-label="Search/Fetch 中文人民币价格来源"]').setValue(
      'https://api-docs.deepseek.com/zh-cn/guides/search/',
    )
    await wrapper.get('input[aria-label="Search/Fetch 英文美元价格来源"]').setValue(
      'https://api-docs.deepseek.com/quick_start/pricing/',
    )
    await wrapper.get('input[aria-label="Search/Fetch 人民币价格核验时间"]').setValue('2026-11-07T08:00')
    await wrapper.get('input[aria-label="Search/Fetch 美元价格核验时间"]').setValue('2026-11-07T08:00')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    const [payload] = api.create.mock.calls[0]
    expect(payload.tool_search_cost_cny_micros_per_success).toBe(5038)
    expect(payload.tool_cny_source_url).toBe('https://api-docs.deepseek.com/zh-cn/guides/search/')
    expect(payload.tool_cny_source_checked_at).toMatch(/Z$/)
    expect(payload.tool_usd_source_url).toBe('https://api-docs.deepseek.com/quick_start/pricing/')
    expect(payload.tool_usd_source_checked_at).toMatch(/Z$/)
    expect(payload.tool_search_price_milli_per_success).toBe(1800)
    expect(payload.tool_fetch_cost_cny_micros_per_success).toBe(0)
    expect(payload.tool_fetch_price_milli_per_success).toBe(0)
    wrapper.unmount()
  })

  it('记录供应商成本上涨并要求在拒绝/取消时写原因', async () => {
    const draft = { ...baseCatalog, id: 3, status: 'draft' as const, created_by: 9,
      supplier_price_diff: { changes: [], increases: [{ model_name: 'deepseek-flash', channel: 'output' as const, previous: 1, current: 2 }], decreases: [] } }
    api.list.mockResolvedValue({ items: [draft] })
    const wrapper = mount(BillingRateCatalog)
    await flushPromises()
    expect(wrapper.text()).toContain('检测到 1 项供应商价格上涨')
    const reject = wrapper.findAll('button').find(button => button.text() === '拒绝草稿')!
    expect((reject.element as HTMLButtonElement).disabled).toBe(true)
    await wrapper.get('input[placeholder="说明价格事实或毛利校验问题"]').setValue('来源核验不一致')
    expect((reject.element as HTMLButtonElement).disabled).toBe(false)
    await reject.trigger('click')
    await flushPromises()
    expect(api.reject).toHaveBeenCalledWith(3, '来源核验不一致', 'catalog-session')
    wrapper.unmount()
  })
})
