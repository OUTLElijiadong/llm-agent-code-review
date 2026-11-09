import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mock = vi.hoisted(() => ({
  batches: vi.fn(), rows: vi.fn(), reviews: vi.fn(), importFile: vi.fn(), fetchFile: vi.fn(), review: vi.fn(), token: 'session-a',
}))
const account = vi.hoisted(() => ({
  profile: { id: 1, username: 'admin', role: 'admin', status: 1 } as { id: number; username: string; role: string; status: number } | null,
  token: 'session-a',
}))
const funds = vi.hoisted(() => ({ list: vi.fn(), rows: vi.fn(), upload: vi.fn(), download: vi.fn() }))
vi.mock('@/api/billing', () => ({
  listBillingStatements: mock.batches, listBillingStatementRows: mock.rows,
  listBillingStatementReviews: mock.reviews, importBillingStatement: mock.importFile,
  fetchBillingStatement: mock.fetchFile,
  reviewBillingStatementRow: mock.review,
}))
vi.mock('@/api/wechatFund', () => ({ listFundBatches: funds.list, listFundRows: funds.rows,
  importFundBill: funds.upload, downloadFundBill: funds.download }))
vi.mock('@/utils/token', () => ({ getToken: () => mock.token }))
vi.mock('@/stores/user', async () => {
  const { reactive } = await import('vue')
  const store = reactive(account)
  return { useUserStore: () => store }
})

import BillingStatements from './BillingStatements.vue'
import { useUserStore } from '@/stores/user'

const batch = {
  id: 7, channel: 'wechat' as const, source_origin: 'manual_upload' as const,
  provider_hash_type: null, provider_hash_value: null, bill_date: '2026-11-05', bill_type: 'ALL',
  file_sha256: 'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789',
  file_name: 'wechat.csv', file_size: 512, parser_version: 'statement-v1', row_count: 1,
  payment_total_fen: 1200, refund_total_fen: 0, received_total_fen: 1199, received_missing_count: 1,
  fee_total_micros: 1_000_000, fee_missing_count: 1, imported_by: 1, imported_at: '2026-11-05T03:00:00Z',
}
const row = {
  id: 17, source_line_number: 2, record_type: 'payment' as const, order_no: 'pr_test_001',
  provider_trade_id: 'wx_trade_001', provider_status: 'SUCCESS', currency: 'CNY', amount_fen: 1200,
  received_fen: 1199, fee_micros: 1_000_000, local_order_status: 'paid', local_amount_fen: 1200,
  local_trade_id: 'wx_trade_001', discrepancy_code: 'matched', review_status: 'unreviewed' as const,
}

beforeEach(() => {
  Object.values(mock).forEach(value => { if (typeof value === 'function') value.mockReset() })
  mock.token = 'session-a'; account.profile = { id: 1, username: 'admin', role: 'admin', status: 1 }; account.token = 'session-a'
  mock.batches.mockResolvedValue({ items: [batch], total: 1, limit: 50, offset: 0 })
  mock.rows.mockResolvedValue({ items: [row], total: 1, limit: 100, offset: 0 })
  mock.reviews.mockResolvedValue({ items: [] })
  mock.importFile.mockResolvedValue({ ...batch, id: 8, file_name: 'wechat-new.csv' })
  mock.fetchFile.mockResolvedValue({ ...batch, id: 9, source_origin: 'wechat_api', provider_hash_type: 'SHA1', provider_hash_value: 'a'.repeat(40) })
  mock.review.mockResolvedValue({ ...row, review_status: 'acknowledge', last_review_note: '渠道与订单金额一致' })
  Object.values(funds).forEach(value => value.mockReset())
  funds.list.mockResolvedValue({ items: [], total: 0 })
})

describe('平台渠道账单对账工作台', () => {
  it('资金账单使用独立视图，不混用订单匹配明细', async () => {
    const wrapper = mount(BillingStatements)
    await flushPromises()
    await wrapper.findAll('.bill-tabs button').find(button => button.text() === '微信资金账单')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('尚未核对订单或余额')
    expect(wrapper.text()).not.toContain('账单支付总额')
    expect(funds.list).toHaveBeenCalledWith('session-a', 0)
    wrapper.unmount()
  })
  it('读取批次和明细，并明确金额汇总和只读账本边界', async () => {
    const wrapper = mount(BillingStatements)
    await flushPromises()
    expect(mock.batches).toHaveBeenCalledWith(undefined, 'session-a', { limit: 50, offset: 0 })
    expect(mock.rows).toHaveBeenCalledWith(7, expect.objectContaining({ limit: 100, offset: 0 }), 'session-a')
    expect(wrapper.text()).toContain('账单支付总额')
    expect(wrapper.text()).toContain('¥12.00')
    expect(wrapper.text()).toContain('1 行未提供应结金额')
    expect(wrapper.text()).toContain('1 个来源行未提供手续费')
    expect(wrapper.text()).toContain('不改账本')
    expect(wrapper.text()).toContain('候选')
    expect(wrapper.text()).toContain('退款渠道不一致')
    expect(wrapper.text()).toContain('渠道退款号不一致')
    expect(wrapper.text()).toContain('渠道与本地退款状态不一致')
    wrapper.unmount()
  })

  it('导入文件后展示返回批次，不把导入说成结算完成', async () => {
    const wrapper = mount(BillingStatements)
    await flushPromises()
    const input = wrapper.get('input[type="file"]')
    const selected = new File(['wx bill'], 'wechat-new.csv', { type: 'text/csv' })
    Object.defineProperty(input.element, 'files', { configurable: true, value: [selected] })
    await input.trigger('change')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(mock.importFile).toHaveBeenCalledWith(expect.objectContaining({
      channel: 'wechat', billDate: expect.any(String), billType: 'ALL', file: selected,
    }), 'session-a')
    expect(wrapper.text()).toContain('账单已解析并匹配')
    expect(wrapper.text()).toContain('原始文件未保存')
    wrapper.unmount()
  })

  it('渠道 API 下载后展示已校验来源并保持只读账本边界', async () => {
    const wrapper = mount(BillingStatements)
    await flushPromises()
    mock.batches.mockResolvedValueOnce({
      items: [{ ...batch, id: 9, source_origin: 'wechat_api', provider_hash_type: 'SHA1',
        provider_hash_value: 'a'.repeat(40) }], total: 1, limit: 50, offset: 0,
    })
    await wrapper.findAll('button').find(button => button.text() === '从渠道 API 下载并匹配')!.trigger('click')
    await flushPromises()
    expect(mock.fetchFile).toHaveBeenCalledWith({ channel: 'wechat', billDate: expect.any(String), billType: 'ALL' }, 'session-a')
    expect(wrapper.text()).toContain('渠道账单已下载并校验')
    expect(wrapper.text()).toContain('微信支付 API')
    expect(wrapper.text()).toContain('渠道摘要 SHA1 已校验')
    expect(wrapper.text()).toContain('账本未被修改')
    wrapper.unmount()
  })

  it('超过 50 个历史批次时可翻页打开较早批次', async () => {
    mock.batches.mockImplementation((_channel, _token, params) => params.offset === 50
      ? Promise.resolve({ items: [{ ...batch, id: 1, file_name: 'older-batch.csv' }], total: 51, limit: 50, offset: 50 })
      : Promise.resolve({ items: [{ ...batch, id: 51, file_name: 'newest-batch.csv' }], total: 51, limit: 50, offset: 0 }))
    const wrapper = mount(BillingStatements)
    await flushPromises()
    expect(wrapper.text()).toContain('第 1 / 2 页')
    await wrapper.findAll('button').find(button => button.text() === '下一页')!.trigger('click')
    await flushPromises()
    expect(mock.batches).toHaveBeenLastCalledWith(undefined, 'session-a', { limit: 50, offset: 50 })
    expect(wrapper.text()).toContain('older-batch.csv')
    expect(wrapper.text()).toContain('第 2 / 2 页')
    wrapper.unmount()
  })

  it('审阅结果只更新复核状态并保留审阅历史', async () => {
    const wrapper = mount(BillingStatements)
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '审阅')!.trigger('click')
    await flushPromises()
    mock.reviews.mockResolvedValueOnce({ items: [{
      id: 1, actor_id: 1, decision: 'acknowledge', note: '渠道与订单金额一致', created_at: '2026-11-05T03:05:00Z',
    }] })
    await wrapper.get('textarea').setValue('渠道流水与本地支付事件一致')
    await wrapper.findAll('button').find(button => button.text() === '标记已知悉')!.trigger('click')
    await flushPromises()
    expect(mock.review).toHaveBeenCalledWith(7, 17, 'acknowledge', '渠道流水与本地支付事件一致', 'session-a')
    expect(wrapper.text()).toContain('支付、退款和积分账本未被修改')
    expect(wrapper.text()).toContain('本地订单')
    expect(wrapper.text()).toContain('历史审阅')
    wrapper.unmount()
  })

  it('切换账号立即清空前一账号批次并重新加载', async () => {
    const wrapper = mount(BillingStatements)
    await flushPromises()
    expect(wrapper.text()).toContain('pr_test_001')
    mock.token = 'session-b'
    useUserStore().token = 'session-b'
    useUserStore().profile = { id: 2, username: 'reviewer', role: 'reviewer', status: 1 }
    await nextTick()
    expect(wrapper.text()).not.toContain('pr_test_001')
    await flushPromises()
    expect(mock.batches).toHaveBeenLastCalledWith(undefined, 'session-b', { limit: 50, offset: 0 })
    wrapper.unmount()
  })
})
