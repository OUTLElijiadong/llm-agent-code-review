import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ list: vi.fn(), detail: vi.fn(), decide: vi.fn() }))
const account = vi.hoisted(() => ({ profile: { id: 2 }, token: 'session-a',
  change: null as null | ((id: number, token: string) => void) }))
vi.mock('@/api/billing', () => ({ listBillingC4Reviews: api.list, getBillingC4Review: api.detail,
  decideBillingReservationReconciliation: api.decide }))
vi.mock('@/utils/token', () => ({ getToken: () => account.token }))
vi.mock('@/stores/user', async () => {
  const { reactive } = await import('vue')
  const store = reactive(account)
  account.change = (id, token) => { store.token = token; store.profile.id = id }
  return { useUserStore: () => store }
})
import BillingC4Reviews from './BillingC4Reviews.vue'

const pending = {
  reservation: { id: 5, status: 'unknown', billable_kind: 'model', model_name: 'deepseek-flash',
    requested_milli: 5000, pricing_version: 'v1' },
  proposal: { id: 8, reservation_id: 5, actor_id: 1, decision: 'proposed', resolution: 'settle',
    provider_request_id: 'req-1', input_tokens: 10, output_tokens: 20, tool_successful_calls: null,
    actual_milli: 100, evidence_reference: '工单 1', evidence_sha256: 'a'.repeat(64), note: '核对', created_at: '2026-11-08T00:00:00Z' },
}

beforeEach(() => {
  Object.values(api).forEach(fn => fn.mockReset())
  account.profile.id = 2; account.token = 'session-a'
  api.list.mockResolvedValue({ items: [pending] }); api.detail.mockResolvedValue(pending)
  api.decide.mockResolvedValue({ reservation: { id: 5, status: 'settled' } })
})

describe('委任的账本复核', () => {
  it('只读取待审清单与详情，填写说明后复核', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = mount(BillingC4Reviews)
    await flushPromises()
    expect(wrapper.text()).toContain('待复核')
    expect(wrapper.text()).not.toContain('提交人工提案')
    await wrapper.get('[data-review="5"]').trigger('click'); await flushPromises()
    expect(api.detail).toHaveBeenCalledWith(5, 'session-a')
    expect(wrapper.text()).toContain('工单 1')
    await wrapper.get('#c4-review-note').setValue('已独立核对')
    await wrapper.get('[data-action="approve"]').trigger('click'); await flushPromises()
    expect(api.decide).toHaveBeenCalledWith(5, 'approve', '已独立核对', 'session-a')
    wrapper.unmount()
  })

  it('自己提交的提案不可审批', async () => {
    api.detail.mockResolvedValue({ ...pending, proposal: { ...pending.proposal, actor_id: 2 } })
    const wrapper = mount(BillingC4Reviews)
    await flushPromises(); await wrapper.get('[data-review="5"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('不能复核自己的提案')
    expect(wrapper.find('[data-action="approve"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('无委任权限时清除待审内容并提供明确状态', async () => {
    api.list.mockRejectedValue({ code: 40300 })
    const wrapper = mount(BillingC4Reviews)
    await flushPromises()
    expect(wrapper.text()).toContain('尚无复核权限')
    expect(wrapper.find('[data-review="5"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('切换账号时不展示旧账号迟到的详情', async () => {
    let resolveOld!: (value: unknown) => void
    api.detail.mockReturnValue(new Promise(resolve => { resolveOld = resolve }))
    const wrapper = mount(BillingC4Reviews)
    await flushPromises(); await wrapper.get('[data-review="5"]').trigger('click'); await flushPromises()
    account.change?.(3, 'session-b')
    await flushPromises(); resolveOld(pending); await flushPromises()
    expect(wrapper.text()).not.toContain('工单 1')
    wrapper.unmount()
  })

  it('最后一页为空时仍能返回上一页', async () => {
    api.list.mockResolvedValueOnce({ items: Array.from({ length: 50 }, (_, i) => ({
      ...pending, reservation: { ...pending.reservation, id: i + 1 },
    })) }).mockResolvedValueOnce({ items: [] }).mockResolvedValueOnce({ items: [pending] })
    const wrapper = mount(BillingC4Reviews)
    await flushPromises(); await wrapper.findAll('button').find(button => button.text() === '下一页')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('暂无待复核提案')
    await wrapper.findAll('button').find(button => button.text() === '上一页')!.trigger('click'); await flushPromises()
    expect(api.list).toHaveBeenLastCalledWith(50, 0, 'session-a')
    wrapper.unmount()
  })
})
