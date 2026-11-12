import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ list: vi.fn(), decide: vi.fn() }))
const account = vi.hoisted(() => ({ profile: { id: 2 }, token: 'review-a',
  change: null as null | ((id: number, token: string) => void) }))
vi.mock('@/api/billing', () => ({ listBillingRefundReviews: api.list, decideBillingRefund: api.decide }))
vi.mock('@/utils/token', () => ({ getToken: () => account.token }))
vi.mock('@/stores/user', async () => {
  const { reactive } = await import('vue'); const store = reactive(account)
  account.change = (id, token) => { store.token = token; store.profile.id = id }
  return { useUserStore: () => store }
})
import BillingRefundReviews from './BillingRefundReviews.vue'

const row = { refund_no: 'refund-1', order_no: 'order-1', product: 'reviewer_month', amount_fen: 4900,
  reason: '服务异常', status: 'manual_review', review_note: '待审核', provider_status: null,
  attempt_count: 0, created_at: '2026-11-01T00:00:00', updated_at: '2026-11-01T00:00:00' }

beforeEach(() => {
  api.list.mockReset(); api.decide.mockReset(); account.change?.(2, 'review-a')
  api.list.mockResolvedValue({ items: [row] }); api.decide.mockResolvedValue({ ...row, status: 'approved' })
})

describe('退款独立审核', () => {
  it('展示服务端待审记录并以当前会话提交审核', async () => {
    api.list.mockResolvedValueOnce({ items: [row] }).mockResolvedValueOnce({ items: [] })
    const wrapper = mount(BillingRefundReviews); await flushPromises()
    expect(api.list).toHaveBeenCalledWith('review-a')
    await wrapper.get('[data-refund="refund-1"]').trigger('click')
    expect(wrapper.text()).toContain('¥49.00')
    await wrapper.get('#refund-review-reason').setValue('已独立核对')
    await wrapper.get('[data-action="approve"]').trigger('click'); await flushPromises()
    expect(api.decide).toHaveBeenCalledWith('refund-1', 'approve', '已独立核对', 'review-a')
    expect(wrapper.text()).toContain('审核已批准')
    expect(wrapper.text()).not.toContain('提交/重试渠道退款')
    wrapper.unmount()
  })

  it('失去授权时清除待审内容', async () => {
    api.list.mockResolvedValueOnce({ items: [row] }).mockRejectedValueOnce({ code: 403 })
    const wrapper = mount(BillingRefundReviews); await flushPromises()
    await wrapper.get('[data-refund="refund-1"]').trigger('click')
    await wrapper.get('[data-action="refresh"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('当前账号无退款审核权限')
    expect(wrapper.text()).not.toContain('refund-1')
    wrapper.unmount()
  })

  it('旧已批准单展示重新审核原因并保持审核入口', async () => {
    api.list.mockResolvedValue({ items: [{ ...row, status: 'approved' }] })
    const wrapper = mount(BillingRefundReviews); await flushPromises()
    await wrapper.get('[data-refund="refund-1"]').trigger('click')
    expect(wrapper.text()).toContain('旧单需重新独立审核，原冻结继续保留')
    expect(wrapper.get('[data-action="approve"]').attributes('disabled')).toBeDefined()
    await wrapper.get('#refund-review-reason').setValue('重新核对')
    expect(wrapper.get('[data-action="approve"]').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('使用服务端游标加载较早的待审记录', async () => {
    api.list.mockResolvedValueOnce({ items: [row], next_before_id: 42 })
      .mockResolvedValueOnce({ items: [{ ...row, refund_no: 'refund-2' }], next_before_id: null })
    const wrapper = mount(BillingRefundReviews); await flushPromises()
    await wrapper.get('[data-action="more"]').trigger('click'); await flushPromises()
    expect(api.list).toHaveBeenLastCalledWith('review-a', 42)
    expect(wrapper.text()).toContain('refund-1')
    expect(wrapper.text()).toContain('refund-2')
    expect(wrapper.find('[data-action="more"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('切换账号时忽略前一账号迟到的列表和审核结果', async () => {
    let finishList!: (value: unknown) => void
    api.list.mockReturnValueOnce(new Promise(resolve => { finishList = resolve })).mockResolvedValue({ items: [] })
    const wrapper = mount(BillingRefundReviews); await flushPromises()
    account.change?.(3, 'review-b'); await flushPromises()
    finishList({ items: [row] }); await flushPromises()
    expect(wrapper.text()).not.toContain('refund-1')
    wrapper.unmount()

    account.change?.(2, 'review-a'); api.list.mockResolvedValue({ items: [row] })
    let finishDecision!: (value: unknown) => void
    api.decide.mockReturnValueOnce(new Promise(resolve => { finishDecision = resolve }))
    const second = mount(BillingRefundReviews); await flushPromises()
    await second.get('[data-refund="refund-1"]').trigger('click')
    await second.get('#refund-review-reason').setValue('核验')
    await second.get('[data-action="approve"]').trigger('click')
    api.list.mockResolvedValue({ items: [] }); account.change?.(3, 'review-b'); await flushPromises()
    finishDecision(row); await flushPromises()
    expect(second.text()).not.toContain('审核已批准')
    expect(second.text()).not.toContain('refund-1')
    second.unmount()
  })
})
