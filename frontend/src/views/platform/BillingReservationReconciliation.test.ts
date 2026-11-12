import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  list: vi.fn(), events: vi.fn(), propose: vi.fn(), decide: vi.fn(), recover: vi.fn(),
}))
const account = vi.hoisted(() => ({
  profile: { id: 1, username: 'platform-admin', role: 'super_admin', status: 1 } as
    { id: number; username: string; role: string; status: number } | null,
  token: 'session-a',
  setProfile: null as ((profile: { id: number; username: string; role: string; status: number } | null) => void) | null,
  setToken: null as ((token: string) => void) | null,
}))
vi.mock('@/api/billing', () => ({
  listBillingReservationReconciliation: api.list,
  listBillingReservationReconciliationEvents: api.events,
  proposeBillingReservationReconciliation: api.propose,
  decideBillingReservationReconciliation: api.decide,
  recoverStaleBillingReservationAttempt: api.recover,
}))
vi.mock('@/utils/token', () => ({ getToken: () => account.token }))
vi.mock('@/stores/user', async () => {
  const { reactive } = await import('vue')
  const store = reactive(account)
  account.setProfile = profile => { store.profile = profile }
  account.setToken = token => { store.token = token }
  return { useUserStore: () => store }
})

import BillingReservationReconciliation from './BillingReservationReconciliation.vue'

async function openReservation(wrapper: ReturnType<typeof mount>, id: number) {
  const row = wrapper.findAll('tbody tr').find(candidate => candidate.find('td strong').text() === `#${id}`)
  if (!row) throw new Error(`missing reservation row #${id}`)
  await row.get('button').trigger('click')
  await flushPromises()
}

const pendingProposal = {
  id: 20, reservation_id: 5, related_event_id: null, actor_id: 1,
  decision: 'proposed' as const, resolution: 'settle' as const,
  provider_request_id: 'resp_verified_5', input_tokens: 10_000, output_tokens: 5_000,
  tool_successful_calls: null,
  actual_milli: 30_000, evidence_reference: 'provider-case-5', evidence_sha256: 'a'.repeat(64),
  note: '供应商记录已核对', created_at: '2026-11-06T03:00:00Z',
}
const unknownReservation = {
  id: 5, request_key_suffix: 'qwer12345678', model_name: 'deepseek-flash', billable_kind: 'model' as const,
  tool_name: null, tool_cost_cny_micros_per_success: null, tool_price_milli_per_success: null,
  tool_successful_calls: null, provider_attempt_started_at: null, status: 'unknown' as const,
  usage_state: 'unknown', user_id: 7, enterprise_id: null, group_id: null, usage_kind: 'member',
  created_at: '2026-11-06T02:00:00Z', requested_milli: 80_000, held_milli: 80_000, actual_milli: null,
  input_tokens: null, output_tokens: null, server_tool_search_calls: 0, server_tool_fetch_calls: 0,
  pricing_version: 'catalog-v1', provider_identity_version: 'deepseek-official-v1',
  has_rate_snapshot: true, has_tool_rate_snapshot: false, has_provider_snapshot: true,
  can_settle: true, can_release_not_sent: true,
  can_recover_stale_attempt: false,
  settlement_block_reasons: [], release_block_reasons: [], pending_proposal: null,
}
const noSnapshotReservation = {
  ...unknownReservation, id: 6, request_key_suffix: 'legacy12345678', model_name: 'legacy-model',
  has_rate_snapshot: false, has_provider_snapshot: false, pricing_version: null,
  provider_identity_version: null, can_settle: false,
  server_tool_search_calls: null, server_tool_fetch_calls: null,
  settlement_block_reasons: [
    '缺少供应商身份快照', '缺少原始费率快照', '缺少供应商 Search/Fetch 用量计数，不能推断为零',
  ],
}
const reservedReservation = {
  ...unknownReservation, id: 7, request_key_suffix: 'active12345678', status: 'reserved' as const,
  can_settle: false, can_release_not_sent: false,
  settlement_block_reasons: ['reserved 状态可能仍在上游处理中'],
  release_block_reasons: ['reserved 状态可能仍在上游处理中'],
}
const searchReservation = {
  ...unknownReservation, id: 8, request_key_suffix: 'search12345678', model_name: 'deepseek-search',
  billable_kind: 'search' as const, tool_name: 'search' as const,
  tool_cost_cny_micros_per_success: 5_038, tool_price_milli_per_success: 1_800,
  provider_attempt_started_at: '2026-11-06T02:01:00Z', server_tool_search_calls: null,
  server_tool_fetch_calls: null, has_tool_rate_snapshot: true, has_rate_snapshot: true,
  can_release_not_sent: false,
}

beforeEach(() => {
  Object.values(api).forEach(value => { if (typeof value === 'function') value.mockReset() })
  account.setProfile?.({ id: 1, username: 'platform-admin', role: 'super_admin', status: 1 })
  account.setToken?.('session-a')
  api.list.mockResolvedValue({ items: [unknownReservation, noSnapshotReservation, reservedReservation], total: 3, limit: 100, offset: 0 })
  api.events.mockResolvedValue({ items: [] })
  api.propose.mockResolvedValue(pendingProposal)
  api.decide.mockResolvedValue({ reservation: { ...unknownReservation, status: 'settled' }, event: { ...pendingProposal, id: 21, decision: 'approved' } })
  api.recover.mockResolvedValue({ ...reservedReservation, status: 'unknown' })
})

describe('平台模型账本人工对账工作台', () => {
  it('明确 reserved 只读，并允许缺少旧快照的 unknown 仅提未发送释放提案', async () => {
    const wrapper = mount(BillingReservationReconciliation)
    await flushPromises()
    expect(api.list).toHaveBeenCalledWith(undefined, 'session-a', { limit: 100, offset: 0 })
    expect(wrapper.text()).toContain('双人复核，按原快照计算')

    await openReservation(wrapper, 6)
    expect(wrapper.text()).toContain('缺失，禁止结算')
    expect(wrapper.text()).toContain('缺少供应商 Search/Fetch 用量计数，不能推断为零')
    expect((wrapper.get('.proposal-form select').element as HTMLSelectElement).value).toBe('release_not_sent')
    expect((wrapper.get('.proposal-form option[value="settle"]').element as HTMLOptionElement).disabled).toBe(true)
    expect(wrapper.text()).toContain('证据证明请求从未发送')
    expect(wrapper.get('button.primary').text()).toContain('提交给另一位获授权管理员复核')

    await wrapper.get('input[placeholder="供应商工单号、账单行号或内部证据编号"]').setValue('legacy-provider-case')
    await wrapper.get('input[placeholder="64 位十六进制摘要"]').setValue('b'.repeat(64))
    await wrapper.get('textarea[placeholder^="说明外部证据来源"]').setValue('证据显示请求未发出')
    await wrapper.get('button.primary').trigger('click')
    await flushPromises()
    expect(api.propose).toHaveBeenCalledWith(6, {
      resolution: 'release_not_sent', evidence_reference: 'legacy-provider-case',
      evidence_sha256: 'b'.repeat(64), note: '证据显示请求未发出',
    }, 'session-a')

    await openReservation(wrapper, 7)
    expect(wrapper.text()).toContain('该调用仍可能在供应商处理中')
    expect(wrapper.text()).not.toContain('提交给另一位平台最高管理员复核')
    wrapper.unmount()
  })

  it('陈旧 Search/Fetch 只转入 unknown，不在恢复动作中释放额度', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    api.list.mockResolvedValue({
      items: [{ ...searchReservation, id: 9, status: 'reserved', can_settle: false,
        can_release_not_sent: false, can_recover_stale_attempt: true,
        settlement_block_reasons: ['reserved 状态可能仍在上游处理中'],
        release_block_reasons: ['reserved 状态可能仍在上游处理中'] }],
      total: 1, limit: 100, offset: 0,
    })
    const wrapper = mount(BillingReservationReconciliation)
    await flushPromises()
    await openReservation(wrapper, 9)
    expect(wrapper.text()).toContain('外呼标记已超过 30 分钟')
    await wrapper.get('button.primary').trigger('click')
    await flushPromises()
    expect(api.recover).toHaveBeenCalledWith(
      9,
      expect.stringContaining('仅转入双人对账，不自动释放额度'),
      'session-a',
    )
    expect(wrapper.text()).toContain('额度仍冻结')
    wrapper.unmount()
    vi.restoreAllMocks()
  })

  it('结算提案随证据提交，列表和详情不展示原始提示词', async () => {
    const wrapper = mount(BillingReservationReconciliation)
    await flushPromises()
    await openReservation(wrapper, 5)
    await wrapper.get('input[placeholder="来自供应商响应或账单的请求 ID"]').setValue('resp_verified_5')
    const tokenInputs = wrapper.findAll('input[type="number"]')
    await tokenInputs[0].setValue('10000')
    await tokenInputs[1].setValue('5000')
    await wrapper.get('input[placeholder="供应商工单号、账单行号或内部证据编号"]').setValue('provider-case-5')
    await wrapper.get('input[placeholder="64 位十六进制摘要"]').setValue('a'.repeat(64))
    await wrapper.get('textarea[placeholder^="说明外部证据来源"]').setValue('对照供应商后台记录')
    await wrapper.findAll('button').find(button => button.text().includes('提交给另一位获授权管理员复核'))!.trigger('click')
    await flushPromises()

    expect(api.propose).toHaveBeenCalledWith(5, {
      resolution: 'settle', provider_request_id: 'resp_verified_5', input_tokens: 10000, output_tokens: 5000,
      evidence_reference: 'provider-case-5', evidence_sha256: 'a'.repeat(64), note: '对照供应商后台记录',
    }, 'session-a')
    expect(wrapper.text()).toContain('提案已提交')
    expect(wrapper.text().toLowerCase()).not.toContain('prompt body')
    wrapper.unmount()
  })

  it('Search 提案输入成功请求次数，不要求模型 Token，并携带计费类别', async () => {
    api.list.mockResolvedValue({ items: [searchReservation], total: 1, limit: 100, offset: 0 })
    const wrapper = mount(BillingReservationReconciliation)
    await flushPromises()
    expect(api.list).toHaveBeenCalledWith(undefined, 'session-a', { limit: 100, offset: 0 })
    await openReservation(wrapper, 8)
    expect(wrapper.text()).toContain('供应商直采 5,038 微元人民币/次')
    expect(wrapper.text()).toContain('用户费率 1.8 积分/次')
    expect(wrapper.findAll('.proposal-form input[type="number"]')).toHaveLength(0)
    await wrapper.get('input[placeholder="来自供应商响应或账单的请求 ID"]').setValue('search-request-8')
    await wrapper.findAll('.proposal-form select')[1].setValue('1')
    await wrapper.get('input[placeholder="供应商工单号、账单行号或内部证据编号"]').setValue('provider-case-search')
    await wrapper.get('input[placeholder="64 位十六进制摘要"]').setValue('e'.repeat(64))
    await wrapper.get('textarea[placeholder^="说明外部证据来源"]').setValue('外部账单确认一次成功请求')
    await wrapper.findAll('button').find(button => button.text().includes('提交给另一位获授权管理员复核'))!.trigger('click')
    await flushPromises()

    expect(api.propose).toHaveBeenCalledWith(8, {
      resolution: 'settle', provider_request_id: 'search-request-8', tool_successful_calls: 1,
      evidence_reference: 'provider-case-search', evidence_sha256: 'e'.repeat(64),
      note: '外部账单确认一次成功请求',
    }, 'session-a')
    const kindFilter = wrapper.findAll('select').find(select => select.text().includes('Search 按次'))
    await kindFilter!.setValue('search')
    await flushPromises()
    expect(api.list).toHaveBeenLastCalledWith(undefined, 'session-a', {
      limit: 100, offset: 0, billable_kind: 'search',
    })
    wrapper.unmount()
  })

  it('无提案的业务阻断显示为空动作且不伪造证据字段', async () => {
    api.events.mockResolvedValue({ items: [{
      id: 30, reservation_id: 5, related_event_id: null, actor_id: 2,
      decision: 'blocked', resolution: null, provider_request_id: null,
      input_tokens: null, output_tokens: null, actual_milli: null,
      evidence_reference: null, evidence_sha256: null,
      note: '没有待复核的对账提案，账本未变更', created_at: '2026-11-06T03:00:00Z',
    }] })
    const wrapper = mount(BillingReservationReconciliation)
    await flushPromises()
    await openReservation(wrapper, 5)
    expect(wrapper.text()).toContain('未关联账本动作')
    expect(wrapper.text()).toContain('此业务阻断事件不含用户提交的证据字段')
    expect(wrapper.text()).toContain('没有待复核的对账提案，账本未变更')
    expect(wrapper.text()).not.toContain('undefined')
    wrapper.unmount()
  })

  it('提案人不能自批，另一位最高管理员可审批并完成后查看审计历史', async () => {
    api.list.mockResolvedValue({
      items: [{ ...unknownReservation, pending_proposal: pendingProposal }], total: 1, limit: 100, offset: 0,
    })
    api.events.mockResolvedValue({ items: [pendingProposal] })
    const wrapper = mount(BillingReservationReconciliation)
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '查看')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('你是提案人，不能审批本人提案')
    expect(wrapper.text()).not.toContain('批准并执行账本变更')

    account.setProfile?.({ id: 2, username: 'second-admin', role: 'super_admin', status: 1 })
    account.setToken?.('session-b')
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '查看')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('按原费率快照计算为 30 积分')
    await wrapper.get('textarea[placeholder="写明证据核验方式和审批依据"]').setValue('已独立复核供应商记录')
    await wrapper.findAll('button').find(button => button.text() === '批准并执行账本变更')!.trigger('click')
    await flushPromises()
    expect(api.decide).toHaveBeenCalledWith(5, 'approve', '已独立复核供应商记录', 'session-b')
    expect(wrapper.text()).toContain('复核通过，账本变更已完成')
    wrapper.unmount()
  })
})
