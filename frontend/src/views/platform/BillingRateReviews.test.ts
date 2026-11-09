import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ list: vi.fn(), get: vi.fn(), decide: vi.fn(), cancellations: vi.fn(), decideCancellation: vi.fn() }))
const account = vi.hoisted(() => ({ profile: { id: 5 }, token: 'review-session', change: null as null | ((id: number, token: string) => void) }))
vi.mock('@/api/billing', () => ({ listBillingRateReviews: api.list, getBillingRateReview: api.get, decideBillingRateReview: api.decide, listBillingRateCancellations: api.cancellations, decideBillingRateCancellation: api.decideCancellation }))
vi.mock('@/utils/token', () => ({ getToken: () => account.token }))
vi.mock('@/stores/user', async () => {
  const { reactive } = await import('vue'); const store = reactive(account)
  account.change = (id, token) => { store.profile.id = id; store.token = token }
  return { useUserStore: () => store }
})
import BillingRateReviews from './BillingRateReviews.vue'
const row = { id: 2, created_by: 9, version_key: 'a'.repeat(64), effective_at: '2027-01-01T00:00:00Z',
  change_note: '价格更新', source_url: 'https://api-docs.deepseek.com/quick_start/pricing/', source_checked_at: '2026-11-08T00:00:00Z',
  model_cny_source_url: null, model_cny_source_checked_at: null, fx_source_url: 'https://www.boc.cn/sourcedb/whpj/index.html',
  fx_source_checked_at: '2026-11-08T00:00:00Z', fx_cny_per_usd: 7, source_sha256: 'b'.repeat(64), credit_pack_price_fen: 400,
  credit_pack_credits: 1000, tool_cny_source_url: null, tool_usd_source_url: null, tool_cny_source_checked_at: null, tool_usd_source_checked_at: null,
  tool_search_cost_cny_micros_per_success: 0, tool_search_price_milli_per_success: 0, tool_fetch_cost_cny_micros_per_success: 0,
  tool_fetch_price_milli_per_success: 0, tool_margin_bps: { search: null, fetch: null }, margin_bps: {}, supplier_price_diff: { increases: [], decreases: [] },
  tool_supplier_price_diff: { increases: [], decreases: [] }, items: [] }
beforeEach(() => { Object.values(api).forEach(fn => fn.mockReset()); account.profile.id = 5; account.token = 'review-session';
  api.cancellations.mockResolvedValue({ items: [] }); api.list.mockResolvedValue({ items: [row] }); api.get.mockResolvedValue(row); api.decide.mockResolvedValue(row) })
describe('费率待复核', () => {
  it('展示真实待审详情并使用独立费率审批接口', async () => {
    const wrapper = mount(BillingRateReviews); await flushPromises()
    await wrapper.get('[data-review="2"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('价格更新')
    await wrapper.get('[data-action="approve"]').trigger('click'); await flushPromises()
    expect(api.decide).toHaveBeenCalledWith(2, 'approve', '', 'review-session')
    wrapper.unmount()
  })
  it('切换账号后不显示旧账号迟到的费率详情', async () => {
    let resolve!: (value: unknown) => void
    api.get.mockReturnValueOnce(new Promise(done => { resolve = done }))
    const wrapper = mount(BillingRateReviews); await flushPromises()
    await wrapper.get('[data-review="2"]').trigger('click'); await flushPromises()
    account.change?.(8, 'new-session'); await flushPromises()
    resolve(row); await flushPromises()
    expect(wrapper.text()).not.toContain('价格更新')
    wrapper.unmount()
  })
  it('取消申请展示目录事实，过期决定不会说成取消成功', async () => {
    api.cancellations.mockResolvedValue({ items: [{ id: 13, catalog_id: 2, status: 'pending', proposed_by: 9,
      proposal_note: '价格来源待核验', proposed_at: '2026-11-08T00:00:00Z', catalog: row }] })
    api.decideCancellation.mockResolvedValue({ id: 13, status: 'expired' })
    const wrapper = mount(BillingRateReviews); await flushPromises()
    await wrapper.get('[data-cancellation="13"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('价格来源待核验')
    expect(wrapper.text()).toContain('价格更新')
    await wrapper.get('#rate-cancel-note').setValue('核验通过')
    await wrapper.get('[data-action="approve-cancel"]').trigger('click'); await flushPromises()
    expect(api.decideCancellation).toHaveBeenCalledWith(13, 'approve', '核验通过', 'review-session')
    expect(wrapper.text()).toContain('申请已过期，费率未取消')
    wrapper.unmount()
  })

  it('不能复核本人取消申请', async () => {
    api.cancellations.mockResolvedValue({ items: [{ id: 13, catalog_id: 2, status: 'pending', proposed_by: 5,
      proposal_note: '申请取消', proposed_at: '2026-11-08T00:00:00Z', catalog: row }] })
    const wrapper = mount(BillingRateReviews); await flushPromises()
    await wrapper.get('[data-cancellation="13"]').trigger('click')
    expect(wrapper.text()).toContain('不能复核自己的取消申请')
    expect(wrapper.find('[data-action="approve-cancel"]').exists()).toBe(false)
    expect(api.decideCancellation).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('不允许自审并在撤权时清除详情', async () => {
    api.get.mockResolvedValueOnce({ ...row, created_by: 5 })
    const wrapper = mount(BillingRateReviews); await flushPromises()
    await wrapper.get('[data-review="2"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('不能复核自己的草稿')
    expect(wrapper.find('[data-action="approve"]').exists()).toBe(false)
    api.list.mockRejectedValueOnce({ code: 40300 })
    await wrapper.get('[data-action="refresh"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('无费率复核权限')
    expect(wrapper.text()).not.toContain('价格更新')
    wrapper.unmount()
  })
})
