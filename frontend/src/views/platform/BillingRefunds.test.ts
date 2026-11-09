import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mock = vi.hoisted(() => ({
  list: vi.fn(), decide: vi.fn(), execute: vi.fn(), reconcile: vi.fn(),
}))
const account = vi.hoisted(() => ({ profile: { id: 1 }, token: null as string | null,
  change: null as null | ((id: number, token: string | null) => void) }))
vi.mock('@/api/billing', () => ({
  listBillingRefunds: mock.list, decideBillingRefund: mock.decide,
  executeBillingRefund: mock.execute, reconcileBillingRefund: mock.reconcile,
}))
vi.mock('@/stores/user', async () => {
  const { reactive } = await import('vue')
  const store = reactive(account)
  account.change = (id, token) => { store.token = token; store.profile.id = id }
  return { useUserStore: () => store }
})
vi.mock('@/utils/token', () => ({ getToken: () => account.token }))

import BillingRefunds from './BillingRefunds.vue'

const manualRefund = {
  refund_no: 'rf_manual_1', order_no: 'pr_reviewer_1', amount_fen: 4900, reason: '订阅服务异常',
  status: 'manual_review', review_note: '已消费积分，需要人工复核', provider_status: null,
  attempt_count: 0, created_at: '2026-11-01T00:00:00', updated_at: '2026-11-01T00:00:00',
}

beforeEach(() => {
  account.change?.(1, null)
  Object.values(mock).forEach(fn => fn.mockReset())
  mock.list.mockResolvedValue([manualRefund])
  mock.decide.mockResolvedValue({ ...manualRefund, status: 'approved' })
  mock.execute.mockResolvedValue({ ...manualRefund, status: 'unknown', provider_status: 'UNKNOWN' })
  mock.reconcile.mockResolvedValue({ ...manualRefund, status: 'succeeded', provider_status: 'SUCCESS' })
})

describe('平台计费与退款工作台', () => {
  it('加载更多保留当前列表并显示更早退款', async () => {
    account.change?.(1, 'admin-a')
    const first = Array.from({ length: 100 }, (_, index) => ({ ...manualRefund, id: 200 - index,
      refund_no: `refund-${200 - index}` }))
    mock.list.mockResolvedValueOnce(first).mockResolvedValueOnce([{ ...manualRefund, id: 100, refund_no: 'older' }])
    const wrapper = mount(BillingRefunds); await flushPromises()
    await wrapper.get('[data-action="more-refunds"]').trigger('click'); await flushPromises()
    expect(mock.list).toHaveBeenLastCalledWith('manual_review', 'admin-a', 101)
    expect(wrapper.text()).toContain('refund-200')
    expect(wrapper.text()).toContain('older')
    expect(wrapper.find('[data-action="more-refunds"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('按 ID 加载更早退款，切换状态后丢弃迟到的旧页', async () => {
    account.change?.(1, 'admin-a')
    const first = Array.from({ length: 100 }, (_, index) => ({ ...manualRefund, id: 200 - index,
      refund_no: `refund-${200 - index}`, status: 'approved' }))
    let finish!: (value: unknown) => void
    mock.list.mockResolvedValueOnce(first).mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
      .mockResolvedValueOnce([{ ...manualRefund, id: 1, refund_no: 'unknown-1', status: 'unknown' }])
    const wrapper = mount(BillingRefunds); await flushPromises()
    await wrapper.get('[data-action="more-refunds"]').trigger('click')
    expect(mock.list).toHaveBeenLastCalledWith('manual_review', 'admin-a', 101)
    await wrapper.get('select').setValue('unknown'); await flushPromises()
    finish([{ ...manualRefund, id: 100, refund_no: 'older-refund' }]); await flushPromises()
    expect(wrapper.text()).toContain('unknown-1')
    expect(wrapper.text()).not.toContain('older-refund')
    wrapper.unmount()
  })

  it('切换账号后丢弃旧账号迟到的退款下一页', async () => {
    account.change?.(1, 'admin-a')
    const first = Array.from({ length: 100 }, (_, index) => ({ ...manualRefund, id: 200 - index,
      refund_no: `old-${200 - index}` }))
    let finish!: (value: unknown) => void
    mock.list.mockResolvedValueOnce(first).mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
      .mockResolvedValue([{ ...manualRefund, id: 7, refund_no: 'new-account' }])
    const wrapper = mount(BillingRefunds); await flushPromises()
    await wrapper.get('[data-action="more-refunds"]').trigger('click')
    account.change?.(2, 'admin-b'); await flushPromises()
    finish([{ ...manualRefund, id: 1, refund_no: 'old-late' }]); await flushPromises()
    expect(wrapper.text()).toContain('new-account')
    expect(wrapper.text()).not.toContain('old-late')
    wrapper.unmount()
  })

  it('切换状态加载失败时不把上一状态的退款显示为当前状态', async () => {
    account.change?.(1, 'admin-a')
    mock.list.mockResolvedValueOnce([{ ...manualRefund, id: 1, refund_no: 'old-status' }])
      .mockRejectedValueOnce(new Error('unavailable'))
    const wrapper = mount(BillingRefunds); await flushPromises()
    await wrapper.get('select').setValue('unknown'); await flushPromises()
    expect(wrapper.text()).toContain('退款列表加载失败')
    expect(wrapper.text()).not.toContain('old-status')
    wrapper.unmount()
  })
  it('切换账号后丢弃旧账号迟到的渠道操作结果', async () => {
    account.change?.(1, 'admin-a')
    const providerSuccess = { ...manualRefund, provider_status: 'SUCCESS' }
    mock.list.mockResolvedValue([providerSuccess])
    let finish!: (value: unknown) => void
    mock.reconcile.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    const wrapper = mount(BillingRefunds); await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '处理')!.trigger('click')
    await wrapper.findAll('button').find(button => button.text() === '重新核验渠道并完成本地退款')!.trigger('click')
    expect(mock.reconcile).toHaveBeenCalledWith(providerSuccess.refund_no, 'admin-a')
    mock.list.mockResolvedValue([]); account.change?.(2, 'admin-b'); await flushPromises()
    finish({ ...providerSuccess, status: 'succeeded' }); await flushPromises()
    expect(wrapper.text()).not.toContain('已查询渠道退款结果')
    expect(wrapper.text()).not.toContain(providerSuccess.refund_no)
    wrapper.unmount()
  })
  it('最高管理员不审核人工退款，仅能委任独立审核人', async () => {
    const wrapper = mount(BillingRefunds)
    await flushPromises()
    expect(wrapper.text()).toContain('rf_manual_1')
    await wrapper.findAll('button').find(button => button.text() === '处理')!.trigger('click')
    expect(wrapper.text()).toContain('等待独立审核')
    expect(wrapper.text()).not.toContain('批准并冻结待退款权益')
    expect(mock.decide).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('管理复核人')
    wrapper.unmount()
  })

  it('对未知退款只提供查单，不提供重新发起渠道退款', async () => {
    mock.list.mockResolvedValueOnce([manualRefund]).mockResolvedValueOnce([
      { ...manualRefund, status: 'unknown', provider_status: 'UNKNOWN' },
    ])
    const wrapper = mount(BillingRefunds)
    await flushPromises()
    await wrapper.get('select').setValue('unknown')
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '处理')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('查询渠道退款结果')
    expect(wrapper.text()).not.toContain('提交/重试渠道退款')
    wrapper.unmount()
  })

  it('渠道成功但本地待复核时只允许重新查渠道以完成本地账本', async () => {
    const providerSuccess = { ...manualRefund, provider_status: 'SUCCESS' }
    mock.list.mockResolvedValue([providerSuccess])
    const wrapper = mount(BillingRefunds)
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '处理')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('重新核验渠道并完成本地退款')
    expect(wrapper.text()).not.toContain('批准并冻结待退款权益')
    expect(wrapper.text()).not.toContain('拒绝退款')
    await wrapper.findAll('button').find(button => button.text() === '重新核验渠道并完成本地退款')!.trigger('click')
    await flushPromises()
    expect(mock.reconcile).toHaveBeenCalledWith(providerSuccess.refund_no, null)
    wrapper.unmount()
  })

  it('渠道仍未知且本地待复核时只允许查单', async () => {
    mock.list.mockResolvedValue([{ ...manualRefund, provider_status: 'UNKNOWN' }])
    const wrapper = mount(BillingRefunds)
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '处理')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('查询渠道退款结果')
    expect(wrapper.text()).not.toContain('批准并冻结待退款权益')
    expect(wrapper.text()).not.toContain('拒绝退款')
    wrapper.unmount()
  })

  it.each(['approved', 'failed'] as const)('历史 %s 且渠道未找到的退款只能查单', async status => {
    const legacy = { ...manualRefund, status, provider_status: 'NOT_FOUND', attempt_count: 1 }
    mock.list.mockResolvedValue([legacy])
    const wrapper = mount(BillingRefunds)
    await flushPromises()
    await wrapper.get('select').setValue(status)
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '处理')!.trigger('click')
    expect(wrapper.text()).toContain('查询渠道退款结果')
    expect(wrapper.text()).not.toContain('提交/重试渠道退款')
    await wrapper.findAll('button').find(button => button.text() === '查询渠道退款结果')!.trigger('click')
    expect(mock.reconcile).toHaveBeenCalledWith(legacy.refund_no, null)
    wrapper.unmount()
  })

  it.each(['approved', 'failed'] as const)('历史 %s 退款缺少终止证据时只提供查单', async status => {
    const legacy = { ...manualRefund, status, provider_status: 'FAILED', attempt_count: 1,
      requires_reconciliation: true }
    mock.list.mockResolvedValue([legacy])
    const wrapper = mount(BillingRefunds)
    await flushPromises()
    await wrapper.get('select').setValue(status)
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '处理')!.trigger('click')
    expect(wrapper.text()).toContain('查询渠道退款结果')
    expect(wrapper.text()).not.toContain('提交/重试渠道退款')
    await wrapper.findAll('button').find(button => button.text() === '查询渠道退款结果')!.trigger('click')
    expect(mock.reconcile).toHaveBeenCalledWith(legacy.refund_no, null)
    wrapper.unmount()
  })

  it('人工复核中渠道未找到时允许查单，不允许重新审核', async () => {
    mock.list.mockResolvedValue([{ ...manualRefund, provider_status: 'NOT_FOUND', attempt_count: 1 }])
    const wrapper = mount(BillingRefunds)
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '处理')!.trigger('click')
    expect(wrapper.text()).toContain('查询渠道退款结果')
    expect(wrapper.text()).not.toContain('等待独立审核')
    wrapper.unmount()
  })

  it('人工复核的历史失败缺少终止证据时仍可查单', async () => {
    mock.list.mockResolvedValue([{ ...manualRefund, provider_status: 'FAILED', attempt_count: 1,
      requires_reconciliation: true }])
    const wrapper = mount(BillingRefunds)
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '处理')!.trigger('click')
    expect(wrapper.text()).toContain('查询渠道退款结果')
    expect(wrapper.text()).not.toContain('等待独立审核')
    wrapper.unmount()
  })

  it('历史退款查单在切换账号后不会显示旧账号结果', async () => {
    account.change?.(1, 'admin-a')
    const legacy = { ...manualRefund, status: 'failed', provider_status: 'NOT_FOUND', attempt_count: 1 }
    mock.list.mockResolvedValue([legacy])
    let finish!: (value: unknown) => void
    mock.reconcile.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    const wrapper = mount(BillingRefunds)
    await flushPromises()
    await wrapper.get('select').setValue('failed')
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '处理')!.trigger('click')
    await wrapper.findAll('button').find(button => button.text() === '查询渠道退款结果')!.trigger('click')
    expect(mock.reconcile).toHaveBeenCalledWith(legacy.refund_no, 'admin-a')
    mock.list.mockResolvedValue([])
    account.change?.(2, 'admin-b')
    await flushPromises()
    finish({ ...legacy, status: 'succeeded' })
    await flushPromises()
    expect(wrapper.text()).not.toContain(legacy.refund_no)
    expect(wrapper.text()).not.toContain('已查询渠道退款结果')
    wrapper.unmount()
  })
})
