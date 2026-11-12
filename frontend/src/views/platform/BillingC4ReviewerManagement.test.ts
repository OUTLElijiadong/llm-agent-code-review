import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ list: vi.fn(), change: vi.fn(), rateList: vi.fn(), rateChange: vi.fn(), refundList: vi.fn(), refundChange: vi.fn(), users: vi.fn() }))
const account = vi.hoisted(() => ({ profile: { id: 1 }, token: 'super-a',
  change: null as null | ((id: number, token: string) => void) }))
vi.mock('@/api/billing', () => ({ listBillingC4Reviewers: api.list, changeBillingC4Reviewer: api.change, listBillingRateReviewers: api.rateList, changeBillingRateReviewer: api.rateChange, listBillingRefundReviewers: api.refundList, changeBillingRefundReviewer: api.refundChange }))
vi.mock('@/api/user', () => ({ getUsers: api.users }))
vi.mock('@/utils/token', () => ({ getToken: () => account.token }))
vi.mock('@/stores/user', async () => {
  const { reactive } = await import('vue'); const store = reactive(account)
  account.change = (id, token) => { store.token = token; store.profile.id = id }
  return { useUserStore: () => store }
})
import BillingC4ReviewerManagement from './BillingC4ReviewerManagement.vue'

beforeEach(() => {
  Object.values(api).forEach(fn => fn.mockReset())
  account.change?.(1, 'super-a')
  api.list.mockResolvedValue({ items: [{ user_id: 3, username: 'manager', nickname: '组长', active: true, eligible: true, granted_at: '2026-11-08', revoked_at: null }], total: 1 })
  api.rateList.mockResolvedValue({ items: [], total: 0 })
  api.rateChange.mockResolvedValue({ user_id: 3, active: true })
  api.refundList.mockResolvedValue({ items: [], total: 0 })
  api.refundChange.mockResolvedValue({ user_id: 3, active: true })
  api.users.mockResolvedValue({ items: [{ id: 3, username: 'manager', role: 'admin', status: 1 }], total: 1 })
  api.change.mockResolvedValue({ user_id: 3, active: true })
})

describe('复核人委任管理', () => {
  it('退款复核人使用独立授权接口', async () => {
    const wrapper = mount(BillingC4ReviewerManagement, { props: { kind: 'refund' } })
    await wrapper.get('[data-action="toggle"]').trigger('click'); await flushPromises()
    expect(api.refundList).toHaveBeenCalledWith(50, 0, 'super-a')
    await wrapper.get('#refund-user-search').setValue('manager')
    await wrapper.get('[data-action="search"]').trigger('click'); await flushPromises()
    await wrapper.get('[data-user="3"]').trigger('click')
    await wrapper.get('#refund-grant-reason').setValue('退款独立复核')
    await wrapper.get('[data-action="grant"]').trigger('click'); await flushPromises()
    expect(api.refundChange).toHaveBeenCalledWith(3, true, '退款独立复核', 'super-a')
    expect(api.change).not.toHaveBeenCalled()
    expect(api.rateChange).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('基于真实授权列表显示状态，并通过用户名搜索选择平台管理员', async () => {
    const wrapper = mount(BillingC4ReviewerManagement)
    await wrapper.get('[data-action="toggle"]').trigger('click'); await flushPromises()
    expect(api.list).toHaveBeenCalledWith(50, 0, 'super-a')
    expect(wrapper.text()).toContain('可复核')
    expect(wrapper.text()).toContain('组长（manager）')
    await wrapper.get('#c4-user-search').setValue('manager')
    await wrapper.get('[data-action="search"]').trigger('click'); await flushPromises()
    await wrapper.get('[data-user="3"]').trigger('click')
    await wrapper.get('#c4-grant-reason').setValue('独立复核')
    await wrapper.get('[data-action="grant"]').trigger('click'); await flushPromises()
    expect(api.users).toHaveBeenCalledWith({ keyword: 'manager', role: 'admin', status: '1', page: 1, page_size: 20 }, 'super-a')
    expect(api.change).toHaveBeenCalledWith(3, true, '独立复核', 'super-a')
    wrapper.unmount()
  })

  it('费率复核人使用独立授权接口，不改变账本复核授权', async () => {
    const wrapper = mount(BillingC4ReviewerManagement, { props: { kind: 'rate' } })
    await wrapper.get('[data-action="toggle"]').trigger('click'); await flushPromises()
    expect(api.rateList).toHaveBeenCalledWith(50, 0, 'super-a')
    expect(api.list).not.toHaveBeenCalled()
    await wrapper.get('#rate-user-search').setValue('manager')
    await wrapper.get('[data-action="search"]').trigger('click'); await flushPromises()
    await wrapper.get('[data-user="3"]').trigger('click')
    await wrapper.get('#rate-grant-reason').setValue('费率独立复核')
    await wrapper.get('[data-action="grant"]').trigger('click'); await flushPromises()
    expect(api.rateChange).toHaveBeenCalledWith(3, true, '费率独立复核', 'super-a')
    expect(api.change).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('费率撤权只撤销费率复核授权', async () => {
    api.rateList.mockResolvedValue({ items: [{ user_id: 3, username: 'manager', nickname: null,
      active: true, eligible: true, granted_at: '2026-11-08', revoked_at: null }], total: 1 })
    const wrapper = mount(BillingC4ReviewerManagement, { props: { kind: 'rate' } })
    await wrapper.get('[data-action="toggle"]').trigger('click'); await flushPromises()
    await wrapper.get('#rate-grant-reason').setValue('职责调整')
    await wrapper.get('[data-revoke="3"]').trigger('click'); await flushPromises()
    expect(api.rateChange).toHaveBeenCalledWith(3, false, '职责调整', 'super-a')
    expect(api.change).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('撤销复核权限需要说明并调用对应账号', async () => {
    const wrapper = mount(BillingC4ReviewerManagement)
    await wrapper.get('[data-action="toggle"]').trigger('click'); await flushPromises()
    await wrapper.get('#c4-grant-reason').setValue('工作调整')
    await wrapper.get('[data-revoke="3"]').trigger('click'); await flushPromises()
    expect(api.change).toHaveBeenCalledWith(3, false, '工作调整', 'super-a')
    wrapper.unmount()
  })

  it('切换账号忽略旧授权列表', async () => {
    let finish!: (value: unknown) => void
    api.list.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
      .mockResolvedValue({ items: [], total: 0 })
    const wrapper = mount(BillingC4ReviewerManagement)
    await wrapper.get('[data-action="toggle"]').trigger('click'); await flushPromises()
    account.change?.(2, 'super-b'); await flushPromises()
    finish({ items: [{ user_id: 999, active: true }], total: 1 }); await flushPromises()
    expect(wrapper.text()).not.toContain('#999')
    wrapper.unmount()
  })

  it('账号当前不可用不能显示为可复核，且失权后清除已显示的名单', async () => {
    api.list.mockResolvedValueOnce({ items: [{ user_id: 7, username: 'old', nickname: null,
      active: true, eligible: false, granted_at: '2026-11-08', revoked_at: null }], total: 1 })
      .mockRejectedValueOnce({ code: 40300 })
    const wrapper = mount(BillingC4ReviewerManagement)
    await wrapper.get('[data-action="toggle"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('已授权，账号当前不可用')
    await wrapper.get('[data-action="toggle"]').trigger('click')
    await wrapper.get('[data-action="toggle"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('当前账号无权管理复核人')
    expect(wrapper.text()).not.toContain('old')
    wrapper.unmount()
  })
})
