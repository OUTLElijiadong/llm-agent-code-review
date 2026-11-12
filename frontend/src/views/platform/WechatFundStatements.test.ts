import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ list: vi.fn(), rows: vi.fn(), upload: vi.fn(), download: vi.fn(), token: 'token-a' }))
const account = vi.hoisted(() => ({ profile: { id: 1 }, token: 'token-a' }))
vi.mock('@/api/wechatFund', () => ({ listFundBatches: api.list, listFundRows: api.rows,
  importFundBill: api.upload, downloadFundBill: api.download }))
vi.mock('@/utils/token', () => ({ getToken: () => api.token }))
vi.mock('@/stores/user', async () => {
  const { reactive } = await import('vue')
  const store = reactive(account)
  return { useUserStore: () => store }
})
import WechatFundStatements from './WechatFundStatements.vue'

const batch = { id: 7, bill_date: '2026-11-05', account_type: 'BASIC', source_origin: 'manual_upload',
  file_sha256: 'a'.repeat(64), file_size: 88, provider_hash_type: null, row_count: 1,
  income_count: 1, income_fen: '9007199254740993', expense_count: 0, expense_fen: '0',
  fee_debit_fen: '0', fee_refund_fen: '0', imported_by: 1, imported_at: '2026-11-06T00:00:00',
  verification: 'file_totals_only' }
const row = { id: 1, source_line_number: 2, booked_at: '2026-11-05T08:00:00',
  provider_business_no: 'wx', funding_flow_no: 'flow', business_name: '交易', business_type: '交易',
  direction: '收入', amount_fen: '9007199254740993', balance_fen: '9007199254740993', business_voucher_no: 'order' }

beforeEach(() => {
  api.token = 'token-a'; account.token = 'token-a'; account.profile = { id: 1 }
  for (const fn of [api.list, api.rows, api.upload, api.download]) fn.mockReset()
  api.list.mockResolvedValue({ items: [batch], total: 1 }); api.rows.mockResolvedValue({ items: [row] })
  api.upload.mockResolvedValue(batch); api.download.mockResolvedValue({ ...batch, source_origin: 'wechat_api', provider_hash_type: 'SHA1' })
})
describe('微信资金账单', () => {
  it('区分手工来源和文件内汇总，金额分值无浮点损失', async () => {
    const wrapper = mount(WechatFundStatements); await flushPromises()
    expect(wrapper.text()).toContain('未验证渠道摘要')
    expect(wrapper.text()).toContain('尚未核对订单或余额')
    expect(wrapper.text()).toContain('90071992547409.93')
    wrapper.unmount()
  })
  it('凭据变化后清除内容并丢弃迟到明细', async () => {
    let resolve!: (value: unknown) => void
    api.rows.mockReturnValueOnce(new Promise(done => { resolve = done }))
    const wrapper = mount(WechatFundStatements); await flushPromises()
    api.token = 'token-b'
    window.dispatchEvent(new StorageEvent('storage', { key: 'review_token', newValue: 'token-b' }))
    resolve({ items: [row] }); await flushPromises()
    expect(wrapper.text()).not.toContain('order')
    wrapper.unmount()
  })
  it('下载保留实际账号和日期参数，不宣称已对账', async () => {
    const wrapper = mount(WechatFundStatements); await flushPromises()
    await wrapper.get('[data-action="download"]').trigger('click'); await flushPromises()
    expect(api.download).toHaveBeenCalledWith(expect.objectContaining({ account: 'BASIC' }), 'token-a')
    expect(wrapper.text()).not.toContain('对账成功')
    wrapper.unmount()
  })
})
