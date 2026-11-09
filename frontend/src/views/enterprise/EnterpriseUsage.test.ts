import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mock = vi.hoisted(() => ({ get: vi.fn(), getLimits: vi.fn(), setLimit: vi.fn(), groupMembers: vi.fn(), setOption: vi.fn(), dispose: vi.fn(), getToken: vi.fn<() => string | null>() }))
vi.mock('@/api/personalUsage', () => ({ getEnterpriseUsage: mock.get, getEnterpriseLimits: mock.getLimits,
  setEnterpriseLimit: mock.setLimit }))
vi.mock('@/api/enterprise', () => ({ listEnterpriseGroupMembers: mock.groupMembers }))
vi.mock('@/utils/token', () => ({ getToken: mock.getToken }))
vi.mock('echarts/core', () => ({ use: vi.fn(), init: vi.fn(() => ({
  setOption: mock.setOption, dispose: mock.dispose, resize: vi.fn(),
})) }))
import EnterpriseUsage from './EnterpriseUsage.vue'

const groups = [{ id: 3, enterprise_id: 10, name: '研发', status: 'active', my_role: 'leader' as const }]
const directory = [{ user_id: 1, display_name: '小李' }, { user_id: 9, display_name: '企业其他成员' }]
const result = { timezone: 'UTC', scope_note: '仅统计已接入商业计量的调用', items: [
  { date: '2026-10-31', input_tokens: 10, output_tokens: 4, reported_calls: 1, unknown_calls: 1,
    pending_calls: 0, charged_milli: 26, server_tool_search_calls: 2, server_tool_fetch_calls: 1,
    server_tool_usage_reported_calls: 1,
    search_success_calls: 1, search_charged_milli: 1_800, search_unknown_calls: 0, search_unknown_milli: 0,
    search_pending_calls: 0, search_pending_milli: 0, search_not_billable_calls: 0,
    fetch_success_calls: 0, fetch_charged_milli: 0, fetch_unknown_calls: 0, fetch_unknown_milli: 0,
    fetch_pending_calls: 0, fetch_pending_milli: 0, fetch_not_billable_calls: 0 },
] }
beforeEach(() => { mock.get.mockReset(); mock.getLimits.mockReset(); mock.setLimit.mockReset(); mock.groupMembers.mockReset(); mock.getToken.mockReturnValue('session-a')
  mock.setOption.mockReset(); mock.get.mockResolvedValue(result); mock.getLimits.mockResolvedValue({ period: '2026-11', timezone: 'Asia/Shanghai', items: [] })
  mock.setLimit.mockResolvedValue({}); mock.groupMembers.mockResolvedValue([{ user_id: 1, role: 'member' }]) })
afterEach(() => { vi.useRealTimers() })

describe('企业 Token 用量', () => {
  it('将筛选和额度字段的标签与对应控件归为一组', async () => {
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises()
    await wrapper.get('#quota-scope').setValue('member')
    for (const id of ['usage-group', 'usage-member', 'usage-period', 'quota-period',
      'quota-scope', 'quota-group', 'quota-member', 'quota-credits']) {
      const label = wrapper.get(`label[for="${id}"]`)
      const field = label.element.closest('.field')
      expect(field?.querySelector(`#${id}`), `${id} 的标签与控件应在同一组`).toBe(wrapper.get(`#${id}`).element)
    }
    wrapper.unmount()
  })

  it('默认展示关键用量和额度，次要指标与逐日表可通过原生详情展开', async () => {
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises()
    const summary = wrapper.get('.summary')
    expect(summary.findAll(':scope > span')).toHaveLength(4)
    expect(summary.text()).toContain('已记录合计')
    expect(summary.text()).toContain('模型扣费积分')
    expect(summary.text()).toContain('未知用量调用')
    expect(summary.text()).toContain('待结算调用')
    const details = wrapper.get('details.usage-details')
    expect(details.attributes('open')).toBeUndefined()
    expect(details.get('summary').text()).toContain('详细数据')
    expect(details.text()).toContain('独立 Search 成功 / 扣费')
    expect(details.text()).toContain('模型内工具计数请求')
    expect(details.find('table').exists()).toBe(true)
    expect(wrapper.find('.quota-form').exists()).toBe(true)
    ;(details.element as HTMLDetailsElement).open = true
    await details.trigger('toggle')
    expect((details.element as HTMLDetailsElement).open).toBe(true)
    wrapper.unmount()
  })

  it('工具有未知费用时在折叠明细外保留状态提示', async () => {
    mock.get.mockResolvedValue({ ...result, items: [{ ...result.items[0], search_unknown_calls: 1, search_unknown_milli: 100 }] })
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises()
    expect(wrapper.get('.pending-tools').text()).toContain('未知或待结算')
    expect(wrapper.get('details.usage-details').text()).toContain('独立 Search 未知（冻结）')
    wrapper.unmount()
  })
  it('跨上海月初恢复页面时更新月份并清除过期输入', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-31T15:59:00Z'))
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises()
    await wrapper.get('#quota-credits').setValue('25')
    vi.setSystemTime(new Date('2026-10-31T16:01:00Z'))
    window.dispatchEvent(new Event('focus')); await flushPromises()
    expect(wrapper.get('#quota-period').findAll('option').map(option => (option.element as HTMLOptionElement).value))
      .toEqual(['2026-11', '2026-12'])
    expect((wrapper.get('#quota-period').element as HTMLSelectElement).value).toBe('2026-11')
    expect((wrapper.get('#quota-credits').element as HTMLInputElement).value).toBe('')
    expect(wrapper.text()).toContain('月份已更新')
    expect(mock.setLimit).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('跨月时已选择的新当前月仍有效，保留输入并更新下月选项', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-31T15:59:00Z'))
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises()
    await wrapper.get('#quota-period').setValue('2026-11')
    await wrapper.get('#quota-credits').setValue('25')
    vi.setSystemTime(new Date('2026-10-31T16:01:00Z'))
    document.dispatchEvent(new Event('visibilitychange')); await flushPromises()
    expect(wrapper.get('#quota-period').findAll('option').map(option => (option.element as HTMLOptionElement).value))
      .toEqual(['2026-11', '2026-12'])
    expect((wrapper.get('#quota-period').element as HTMLSelectElement).value).toBe('2026-11')
    expect((wrapper.get('#quota-credits').element as HTMLInputElement).value).toBe('25')
    wrapper.unmount()
  })

  it('跨月后未触发恢复事件就保存时拒绝旧月份的输入', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-31T15:59:00Z'))
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises()
    await wrapper.get('#quota-credits').setValue('25')
    vi.setSystemTime(new Date('2026-10-31T16:01:00Z'))
    await wrapper.get('.quota-form').trigger('submit'); await flushPromises()
    expect(mock.setLimit).not.toHaveBeenCalled()
    expect((wrapper.get('#quota-credits').element as HTMLInputElement).value).toBe('')
    expect(wrapper.text()).toContain('月份已更新')
    wrapper.unmount()
  })

  it('显式刷新时跨月更新月份选项', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-31T15:59:00Z'))
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises()
    vi.setSystemTime(new Date('2026-10-31T16:01:00Z'))
    await wrapper.get('[data-action="refresh-usage"]').trigger('click'); await flushPromises()
    expect(wrapper.get('#quota-period').findAll('option').map(option => (option.element as HTMLOptionElement).value))
      .toEqual(['2026-11', '2026-12'])
    wrapper.unmount()
  })

  it('跨月提示不会带到切换后的企业或账号', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-31T15:59:00Z'))
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises()
    await wrapper.get('#quota-credits').setValue('25')
    vi.setSystemTime(new Date('2026-10-31T16:01:00Z'))
    window.dispatchEvent(new Event('focus')); await flushPromises()
    expect(wrapper.text()).toContain('月份已更新')
    await wrapper.setProps({ enterpriseId: 20, groups: [] }); await flushPromises()
    expect(wrapper.text()).not.toContain('月份已更新')
    wrapper.unmount()
    mock.getToken.mockReturnValue('session-b')
    const next = mount(EnterpriseUsage, { props: { enterpriseId: 20, role: 'owner', groups: [], directory: [] } })
    await flushPromises()
    expect(next.text()).not.toContain('月份已更新')
    expect((next.get('#quota-period').element as HTMLSelectElement).value).toBe('2026-11')
    next.unmount()
  })

  it('保存成功后的额度刷新期间切换小组仍标明原目标', async () => {
    const twoGroups = [...groups, { ...groups[0], id: 4, name: '测试组' }]
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups: twoGroups, directory } })
    await flushPromises()
    await wrapper.get('#quota-scope').setValue('group')
    await wrapper.get('#quota-group').setValue('3'); await flushPromises()
    await wrapper.get('#quota-credits').setValue('25')
    let resolveRefresh!: (value: unknown) => void
    mock.getLimits.mockReturnValueOnce(new Promise(resolve => { resolveRefresh = resolve }))
    await wrapper.get('.quota-form').trigger('submit')
    await Promise.resolve()
    await wrapper.get('#quota-group').setValue('4')
    resolveRefresh({ items: [] }); await flushPromises()
    expect(wrapper.text()).toContain('研发的额度已保存')
    expect(mock.setLimit).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('保存成功后的刷新期间切账号不显示旧账号的结果', async () => {
    const twoGroups = [...groups, { ...groups[0], id: 4, name: '测试组' }]
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups: twoGroups, directory } })
    await flushPromises()
    await wrapper.get('#quota-scope').setValue('group')
    await wrapper.get('#quota-group').setValue('3'); await flushPromises()
    await wrapper.get('#quota-credits').setValue('25')
    let resolveRefresh!: (value: unknown) => void
    mock.getLimits.mockReturnValueOnce(new Promise(resolve => { resolveRefresh = resolve }))
    await wrapper.get('.quota-form').trigger('submit'); await Promise.resolve()
    mock.getToken.mockReturnValue('session-b')
    await wrapper.get('#quota-group').setValue('4')
    resolveRefresh({ items: [] }); await flushPromises()
    expect(wrapper.text()).not.toContain('研发的额度已保存')
    expect(mock.setLimit).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
  it('管理员刷新相同小组列表时保留额度范围、小组和未保存输入', async () => {
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises()
    await wrapper.get('#quota-scope').setValue('group')
    await wrapper.get('#quota-group').setValue('3')
    await wrapper.get('#quota-credits').setValue('25')
    await wrapper.setProps({ groups: groups.map(group => ({ ...group })) })
    await flushPromises()
    expect((wrapper.get('#quota-scope').element as HTMLSelectElement).value).toBe('group')
    expect((wrapper.get('#quota-group').element as HTMLSelectElement).value).toBe('3')
    expect((wrapper.get('#quota-credits').element as HTMLInputElement).value).toBe('25')
    await wrapper.get('.quota-form').trigger('submit'); await flushPromises()
    expect(mock.setLimit).toHaveBeenLastCalledWith(10, expect.objectContaining({ scope: 'group', group_id: 3, limit_credits: 25 }), 'session-a')
    wrapper.unmount()
  })

  it('组长刷新相同小组列表时保留所选小组及额度输入', async () => {
    const twoGroups = [...groups, { ...groups[0], id: 4, name: '测试组' }]
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'member', groups: twoGroups, directory } })
    await flushPromises()
    await wrapper.get('#quota-group').setValue('4')
    await wrapper.get('#quota-credits').setValue('25')
    await wrapper.setProps({ groups: twoGroups.map(group => ({ ...group })) }); await flushPromises()
    expect((wrapper.get('#quota-group').element as HTMLSelectElement).value).toBe('4')
    expect((wrapper.get('#quota-credits').element as HTMLInputElement).value).toBe('25')
    wrapper.unmount()
  })

  it('所选小组失效或切换企业时清除额度输入，避免误存到新范围', async () => {
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises()
    await wrapper.get('#quota-scope').setValue('group')
    await wrapper.get('#quota-group').setValue('3')
    await wrapper.get('#quota-credits').setValue('25')
    await wrapper.setProps({ groups: [] }); await flushPromises()
    expect((wrapper.get('#quota-credits').element as HTMLInputElement).value).toBe('')
    expect(wrapper.get('.quota-form button[type="submit"]').attributes('disabled')).toBeDefined()
    await wrapper.setProps({ enterpriseId: 20, groups: [{ ...groups[0], id: 4, enterprise_id: 20 }] }); await flushPromises()
    expect((wrapper.get('#quota-credits').element as HTMLInputElement).value).toBe('')
    wrapper.unmount()
  })

  it('角色变化时清除旧角色的额度输入和范围', async () => {
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises()
    await wrapper.get('#quota-credits').setValue('25')
    await wrapper.setProps({ role: 'member' }); await flushPromises()
    expect((wrapper.get('#quota-scope').element as HTMLSelectElement).value).toBe('group')
    expect((wrapper.get('#quota-credits').element as HTMLInputElement).value).toBe('')
    wrapper.unmount()
  })

  it('保存进行中刷新小组列表仍向原小组提交，并正确刷新结果', async () => {
    let resolveSave!: (value: unknown) => void
    mock.setLimit.mockReturnValueOnce(new Promise(resolve => { resolveSave = resolve }))
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises()
    await wrapper.get('#quota-scope').setValue('group')
    await wrapper.get('#quota-group').setValue('3')
    await wrapper.get('#quota-credits').setValue('25')
    await wrapper.get('.quota-form').trigger('submit')
    await wrapper.setProps({ groups: groups.map(group => ({ ...group })) }); await flushPromises()
    expect((wrapper.get('#quota-scope').element as HTMLSelectElement).value).toBe('group')
    resolveSave({}); await flushPromises()
    expect(mock.setLimit).toHaveBeenCalledWith(10, expect.objectContaining({ scope: 'group', group_id: 3 }), 'session-a')
    expect(mock.getLimits).toHaveBeenLastCalledWith(10, expect.any(String), undefined, 'session-a')
    wrapper.unmount()
  })

  it('额度查询失败时显示错误，不把未知数据误报为空', async () => {
    mock.getLimits.mockRejectedValueOnce(new Error('offline'))
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises()
    expect(wrapper.text()).toContain('月度额度加载失败')
    expect(wrapper.text()).not.toContain('该月份尚未设置企业、小组或成员额度')
    wrapper.unmount()
  })
  it('显式刷新会重试额度和小组成员而不会重复提交保存', async () => {
    mock.getLimits.mockRejectedValue(new Error('offline'))
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'member', groups, directory } })
    await flushPromises()
    expect(wrapper.text()).toContain('月度额度加载失败')
    mock.getLimits.mockReset().mockResolvedValue({ items: [] }); mock.groupMembers.mockClear(); mock.setLimit.mockClear()
    await wrapper.get('[data-action="refresh-usage"]').trigger('click'); await flushPromises()
    expect(mock.getLimits).toHaveBeenCalledTimes(1)
    expect(mock.getLimits).toHaveBeenCalledWith(10, expect.any(String), 3, 'session-a')
    expect(mock.groupMembers).toHaveBeenCalledWith(10, 3, 'session-a')
    expect(wrapper.text()).not.toContain('月度额度加载失败')
    expect(mock.setLimit).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('跨月刷新时由月份监听器只请求一次新月额度', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-31T15:59:00Z'))
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises(); mock.getLimits.mockClear()
    vi.setSystemTime(new Date('2026-10-31T16:01:00Z'))
    await wrapper.get('[data-action="refresh-usage"]').trigger('click'); await flushPromises()
    expect(mock.getLimits).toHaveBeenCalledTimes(1)
    expect(mock.getLimits).toHaveBeenCalledWith(10, '2026-11', undefined, 'session-a')
    wrapper.unmount()
  })
  it('额度重试与用量重试独立，额度失败不隐藏成功的用量', async () => {
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises()
    mock.getLimits.mockRejectedValueOnce(new Error('offline'))
    await wrapper.get('[data-action="refresh-usage"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('月度额度加载失败')
    expect(wrapper.text()).toContain('已记录合计')
    wrapper.unmount()
  })
  it('刷新期间切换账号和企业时丢弃旧额度及成员的迟到结果', async () => {
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'member', groups, directory } })
    await flushPromises()
    let resolveOldLimits!: (value: unknown) => void
    let resolveOldMembers!: (value: unknown) => void
    mock.getLimits.mockReturnValueOnce(new Promise(resolve => { resolveOldLimits = resolve }))
    // 刷新会分别读取额度成员和用量成员，两者均使用原账号快照。
    mock.groupMembers.mockImplementation((enterpriseId: number) => enterpriseId === 10
      ? new Promise(resolve => { resolveOldMembers = resolve }) : Promise.resolve([]))
    await wrapper.get('[data-action="refresh-usage"]').trigger('click')
    mock.getToken.mockReturnValue('session-b')
    await wrapper.setProps({ enterpriseId: 20, groups: [{ ...groups[0], id: 4, enterprise_id: 20 }], directory: [] })
    await flushPromises()
    resolveOldLimits({ items: [{ scope: 'group', group_id: 3, member_id: null,
      used_milli: 999000, reserved_milli: 0, limit_milli: 999000, remaining_milli: 0 }] })
    resolveOldMembers([{ user_id: 999, role: 'member' }]); await flushPromises()
    expect(wrapper.text()).not.toContain('999')
    expect(wrapper.emitted('access-lost')).toBeUndefined()
    expect(mock.getLimits).toHaveBeenLastCalledWith(20, expect.any(String), 4, 'session-b')
    wrapper.unmount()
  })
  it('所有查询和保存均携带发起时认证快照', async () => {
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'member', groups, directory } })
    await flushPromises()
    expect(mock.get).toHaveBeenCalledWith(10, 30, 3, undefined, 'session-a')
    expect(mock.getLimits).toHaveBeenCalledWith(10, expect.any(String), 3, 'session-a')
    expect(mock.groupMembers).toHaveBeenCalledWith(10, 3, 'session-a')
    await wrapper.get('#quota-credits').setValue('10')
    await wrapper.get('.quota-form').trigger('submit')
    expect(mock.setLimit).toHaveBeenCalledWith(10, expect.objectContaining({ scope: 'group', group_id: 3 }), 'session-a')
    wrapper.unmount()
  })

  it.each(['success', 'failure'] as const)('账号切换后忽略旧额度保存的迟到%s', async outcome => {
    let resolveSave!: (value: unknown) => void
    let rejectSave!: (reason: unknown) => void
    mock.setLimit.mockReturnValueOnce(new Promise((resolve, reject) => { resolveSave = resolve; rejectSave = reject }))
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'member', groups, directory } })
    await flushPromises()
    await wrapper.get('#quota-credits').setValue('10')
    await wrapper.get('.quota-form').trigger('submit')
    mock.getLimits.mockClear()
    mock.getToken.mockReturnValue('session-b')
    if (outcome === 'success') resolveSave({})
    else rejectSave({ code: 40300 })
    await flushPromises()
    expect(mock.getLimits).not.toHaveBeenCalled()
    expect(wrapper.emitted('access-lost')).toBeUndefined()
    expect(wrapper.text()).not.toContain('当前范围的权限已变化')
    expect(wrapper.text()).not.toContain('的额度已保存')
    wrapper.unmount()
  })

  it.each(['success', 'failure'] as const)('账号切换后不显示旧用量查询的迟到%s', async outcome => {
    let resolveUsage!: (value: unknown) => void
    let rejectUsage!: (reason: unknown) => void
    mock.get.mockReturnValueOnce(new Promise((resolve, reject) => { resolveUsage = resolve; rejectUsage = reject }))
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises()
    mock.getToken.mockReturnValue('session-b')
    if (outcome === 'success') resolveUsage(result)
    else rejectUsage({ code: 40300 })
    await flushPromises()
    expect(wrapper.text()).not.toContain('已记录合计')
    expect(wrapper.emitted('access-lost')).toBeUndefined()
    wrapper.unmount()
  })

  it.each(['success', 'failure'] as const)('账号切换后忽略旧额度和小组成员查询的迟到%s', async outcome => {
    let resolveLimits!: (value: unknown) => void
    let rejectLimits!: (reason: unknown) => void
    mock.getLimits.mockReturnValueOnce(new Promise((resolve, reject) => { resolveLimits = resolve; rejectLimits = reject }))
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'member', groups, directory } })
    await flushPromises()
    mock.getToken.mockReturnValue('session-b')
    if (outcome === 'success') resolveLimits({ period: '2026-11', timezone: 'Asia/Shanghai', items: [
      { scope: 'group', group_id: 3, member_id: null, used_milli: 999000,
        reserved_milli: 0, limit_milli: 999000, remaining_milli: 0 },
    ] })
    else rejectLimits({ code: 40300 })
    await flushPromises()
    expect(wrapper.text()).not.toContain('999')
    expect(wrapper.emitted('access-lost')).toBeUndefined()
    wrapper.unmount()
  })

  it.each(['success', 'failure'] as const)('账号切换后忽略旧用量筛选成员的迟到%s', async outcome => {
    let resolveMembers!: (value: unknown) => void
    let rejectMembers!: (reason: unknown) => void
    mock.groupMembers.mockReturnValue(new Promise((resolve, reject) => { resolveMembers = resolve; rejectMembers = reject }))
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'member', groups, directory } })
    await flushPromises()
    mock.getToken.mockReturnValue('session-b')
    if (outcome === 'success') resolveMembers([{ user_id: 999, role: 'member' }])
    else rejectMembers({ code: 40300 })
    await flushPromises()
    expect(wrapper.get('#usage-member').text()).not.toContain('999')
    expect(wrapper.emitted('access-lost')).toBeUndefined()
    wrapper.unmount()
  })

  it('管理员可以选择企业、小组和成员，保留未知用量提示', async () => {
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory, directoryHasMore: true } })
    await flushPromises()
    expect(mock.get).toHaveBeenCalledWith(10, 30, undefined, undefined, 'session-a')
    expect(wrapper.text()).toContain('14')
    expect(wrapper.text()).toContain('仅统计已接入商业计量的调用')
    expect(wrapper.text()).toContain('独立 Search 成功 / 扣费')
    expect(wrapper.text()).toContain('独立 Fetch 成功 / 扣费')
    expect(wrapper.text()).toContain('模型内工具计数请求')
    expect(wrapper.text()).toContain('模型扣费积分')
    expect(wrapper.text()).toContain('0.026')
    expect(wrapper.text()).toContain('2')
    expect(mock.getLimits).toHaveBeenCalledWith(10, expect.any(String), undefined, 'session-a')
    await wrapper.get('#quota-credits').setValue('25')
    await wrapper.get('.quota-form').trigger('submit')
    await flushPromises()
    expect(mock.setLimit).toHaveBeenCalledWith(10, expect.objectContaining({ scope: 'enterprise', limit_credits: 25 }), 'session-a')
    await wrapper.get('#quota-scope').setValue('group')
    expect(wrapper.get('.quota-form button[type="submit"]').attributes('disabled')).toBeDefined()
    await wrapper.get('#quota-group').setValue('3')
    await wrapper.get('.quota-form').trigger('submit')
    await flushPromises()
    expect(mock.setLimit).toHaveBeenLastCalledWith(10, expect.objectContaining({ scope: 'group', group_id: 3 }), 'session-a')
    await wrapper.findAll('button').find(item => item.text() === '加载更多成员')!.trigger('click')
    expect(wrapper.emitted('load-more-members')).toHaveLength(1)
    await wrapper.get('#usage-group').setValue('3'); await flushPromises()
    expect(wrapper.get('#usage-member').findAll('option').map(option => option.element.value)).toEqual(['0', '1'])
    await wrapper.get('#usage-member').setValue('1'); await flushPromises()
    expect(mock.get).toHaveBeenLastCalledWith(10, 30, 3, 1, 'session-a')
    wrapper.unmount()
  })

  it('小组长只能选择自己管理的小组，切企业后丢弃旧结果', async () => {
    let resolveOld!: (value: unknown) => void
    mock.get.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve })).mockResolvedValue(result)
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'member', groups, directory } })
    await flushPromises()
    expect(mock.get).toHaveBeenCalledWith(10, 30, 3, undefined, 'session-a')
    await wrapper.setProps({ enterpriseId: 20, groups: [{ ...groups[0], id: 4, enterprise_id: 20 }] })
    await flushPromises()
    resolveOld({ ...result, items: [{ ...result.items[0], input_tokens: 999999 }] }); await flushPromises()
    expect(wrapper.text()).not.toContain('999,999')
    expect(mock.get).toHaveBeenLastCalledWith(20, 30, 4, undefined, 'session-a')
    wrapper.unmount()
  })

  it('小组长只给自己组设额度，不能提交企业额度或跨组成员', async () => {
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'member', groups, directory: [
      { user_id: 1, display_name: '组内员工' }, { user_id: 9, display_name: '其他组员工' },
    ] } })
    await flushPromises()
    expect(wrapper.find('#quota-scope').findAll('option').map(option => option.element.value)).toEqual(['group', 'member'])
    expect(mock.getLimits).toHaveBeenCalledWith(10, expect.any(String), 3, 'session-a')
    await wrapper.get('#quota-scope').setValue('member')
    await wrapper.get('#quota-credits').setValue('10')
    await wrapper.get('#quota-member').setValue('1')
    await wrapper.get('.quota-form').trigger('submit')
    await flushPromises()
    expect(mock.setLimit).toHaveBeenCalledWith(10, expect.objectContaining({ scope: 'member', group_id: 3, member_id: 1 }), 'session-a')
    expect(wrapper.get('#usage-member').text()).not.toContain('其他组员工')
    expect(wrapper.get('#quota-member').text()).not.toContain('其他组员工')
    wrapper.unmount()
  })

  it('小组长保存小组额度时发送所选小组', async () => {
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'member', groups, directory } })
    await flushPromises()
    await wrapper.get('#quota-credits').setValue('10')
    await wrapper.get('.quota-form').trigger('submit')
    await flushPromises()
    expect(mock.setLimit).toHaveBeenCalledWith(10, expect.objectContaining({ scope: 'group', group_id: 3 }), 'session-a')
    wrapper.unmount()
  })

  it('组长切换额度小组后请求所选组并丢弃旧组迟到响应', async () => {
    let resolveOld!: (value: unknown) => void
    mock.getLimits.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve }))
      .mockResolvedValue({ period: '2026-11', timezone: 'Asia/Shanghai', items: [] })
    const twoGroups = [...groups, { ...groups[0], id: 4, name: '测试组' }]
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'member', groups: twoGroups, directory } })
    await flushPromises()
    await wrapper.get('#quota-group').setValue('4'); await flushPromises()
    expect(mock.getLimits).toHaveBeenLastCalledWith(10, expect.any(String), 4, 'session-a')
    resolveOld({ period: '2026-11', timezone: 'Asia/Shanghai', items: [
      { scope: 'group', group_id: 3, member_id: null, used_milli: 999000,
        reserved_milli: 0, limit_milli: 999000, remaining_milli: 0 },
    ] })
    await flushPromises()
    expect(wrapper.text()).not.toContain('999')
    await wrapper.get('#quota-credits').setValue('10')
    await wrapper.get('.quota-form').trigger('submit'); await flushPromises()
    expect(mock.setLimit).toHaveBeenLastCalledWith(10, expect.objectContaining({ scope: 'group', group_id: 4 }), 'session-a')
    wrapper.unmount()
  })

  it('切换额度小组后将旧组保存的不确定结果标明范围', async () => {
    let rejectSave!: (value: unknown) => void
    mock.setLimit.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectSave = reject }))
    const twoGroups = [...groups, { ...groups[0], id: 4, name: '测试组' }]
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'member', groups: twoGroups, directory } })
    await flushPromises()
    await wrapper.get('#quota-credits').setValue('10')
    await wrapper.get('.quota-form').trigger('submit')
    await wrapper.get('#quota-group').setValue('4'); await flushPromises()
    rejectSave(new Error('old group failure')); await flushPromises()
    expect(wrapper.text()).toContain('研发的额度保存结果未知，请切换回原范围刷新核对')
    wrapper.unmount()
  })

  it('切换额度小组后标明旧组保存成功的结果', async () => {
    let resolveSave!: (value: unknown) => void
    mock.setLimit.mockReturnValueOnce(new Promise(resolve => { resolveSave = resolve }))
    const twoGroups = [...groups, { ...groups[0], id: 4, name: '测试组' }]
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'member', groups: twoGroups, directory } })
    await flushPromises()
    await wrapper.get('#quota-credits').setValue('10')
    await wrapper.get('.quota-form').trigger('submit')
    await wrapper.get('#quota-group').setValue('4'); await flushPromises()
    resolveSave({}); await flushPromises()
    expect(wrapper.text()).toContain('研发的额度已保存')
    wrapper.unmount()
  })

  it('保存中切换额度范围时仍标明原范围的结果', async () => {
    let resolveSave!: (value: unknown) => void
    mock.setLimit.mockReturnValueOnce(new Promise(resolve => { resolveSave = resolve }))
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises()
    await wrapper.get('#quota-scope').setValue('group')
    await wrapper.get('#quota-group').setValue('3')
    await wrapper.get('#quota-credits').setValue('10')
    await wrapper.get('.quota-form').trigger('submit')
    await wrapper.get('#quota-scope').setValue('enterprise'); await flushPromises()
    resolveSave({}); await flushPromises()
    expect(wrapper.text()).toContain('研发的额度已保存')
    wrapper.unmount()
  })

  it.each(['success', 'failure'] as const)('保存中切换成员时标明原成员的迟到%s', async outcome => {
    let resolveSave!: (value: unknown) => void
    let rejectSave!: (reason: unknown) => void
    mock.setLimit.mockReturnValueOnce(new Promise((resolve, reject) => { resolveSave = resolve; rejectSave = reject }))
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises()
    await wrapper.get('#quota-scope').setValue('member')
    await wrapper.get('#quota-member').setValue('1')
    await wrapper.get('#quota-credits').setValue('10')
    await wrapper.get('.quota-form').trigger('submit')
    await wrapper.get('#quota-member').setValue('9'); await flushPromises()
    if (outcome === 'success') resolveSave({})
    else rejectSave(new Error('request outcome unknown'))
    await flushPromises()
    expect(wrapper.text()).toContain(outcome === 'success' ? '小李的额度已保存' : '小李的额度保存结果未知')
    wrapper.unmount()
  })

  it('保存中切换月份时标明原月份和原目标', async () => {
    let resolveSave!: (value: unknown) => void
    mock.setLimit.mockReturnValueOnce(new Promise(resolve => { resolveSave = resolve }))
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises()
    const options = wrapper.get('#quota-period').findAll('option')
    const originalPeriod = (options[0].element as HTMLOptionElement).value
    const nextPeriod = (options[1].element as HTMLOptionElement).value
    await wrapper.get('#quota-scope').setValue('group')
    await wrapper.get('#quota-group').setValue('3')
    await wrapper.get('#quota-credits').setValue('10')
    await wrapper.get('.quota-form').trigger('submit')
    await wrapper.get('#quota-period').setValue(nextPeriod); await flushPromises()
    resolveSave({}); await flushPromises()
    expect(wrapper.get('[role="status"]').text()).toContain('研发的额度已保存')
    expect(wrapper.get('[role="status"]').text()).toContain(originalPeriod)
    wrapper.unmount()
  })

  it('权限撤销时立即隐藏已有数据并通知父页面', async () => {
    mock.get.mockResolvedValueOnce(result).mockRejectedValueOnce({ code: 40300 })
    const wrapper = mount(EnterpriseUsage, { props: { enterpriseId: 10, role: 'owner', groups, directory } })
    await flushPromises()
    await wrapper.get('[data-action="refresh-usage"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).not.toContain('14')
    expect(wrapper.emitted('access-lost')).toHaveLength(1)
    wrapper.unmount()
  })
})
