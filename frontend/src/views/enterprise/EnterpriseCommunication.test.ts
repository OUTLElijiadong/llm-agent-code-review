import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'

const mock = vi.hoisted(() => ({
  listMyEnterprises: vi.fn(), acceptEnterpriseInvitation: vi.fn(), listEnterpriseGroups: vi.fn(),
  listEnterpriseDirectory: vi.fn(), listEnterpriseConversations: vi.fn(), openEnterpriseConversation: vi.fn(),
  getEnterpriseAssistantBillingContext: vi.fn(),
  getEnterpriseMessages: vi.fn(), sendEnterpriseMessage: vi.fn(), requestEnterpriseAssistant: vi.fn(),
  listEnterpriseAssistantRuns: vi.fn(), getEnterpriseAssistantRun: vi.fn(), retryEnterpriseAssistantRun: vi.fn(),
  getEnterpriseAssistantClarification: vi.fn(), decideEnterpriseAssistantClarification: vi.fn(),
  resumeEnterpriseAssistantClarification: vi.fn(),
  answerEnterpriseAssistantRun: vi.fn(), cancelEnterpriseAssistantRun: vi.fn(),
  listEnterpriseAssistantApprovals: vi.fn(), decideEnterpriseAssistantApproval: vi.fn(),
  listEnterpriseProjects: vi.fn(), listEnterpriseRemoteApprovals: vi.fn(),
  submitEnterpriseRemoteApproval: vi.fn(), decideEnterpriseRemoteApproval: vi.fn(),
  cancelEnterpriseRemoteApproval: vi.fn(), startEnterpriseRemoteApproval: vi.fn(),
  listEnterprisePosts: vi.fn(), listEnterpriseAnnouncements: vi.fn(),
  getEnterprisePost: vi.fn(), createEnterprisePost: vi.fn(), updateEnterprisePost: vi.fn(),
  deleteEnterprisePost: vi.fn(), replyEnterprisePost: vi.fn(), deleteEnterpriseReply: vi.fn(),
  createEnterpriseAnnouncement: vi.fn(), updateEnterpriseAnnouncement: vi.fn(), deleteEnterpriseAnnouncement: vi.fn(),
}))
const account = vi.hoisted(() => ({ profile: { id: 1 } }))
const workspaceState = vi.hoisted(() => ({
  activeSpace: 'enterprise',
  activeEnterpriseId: null as number | null,
  activateEnterprise: vi.fn((id?: number | null) => { workspaceState.activeEnterpriseId = id ?? null }),
}))
const initialViewport = { width: window.innerWidth, height: window.innerHeight }
vi.mock('@/api/enterprise', () => mock)
vi.mock('@/stores/user', async () => {
  const { reactive } = await import('vue')
  const store = reactive(account)
  return { useUserStore: () => store }
})
vi.mock('@/stores/workspace', () => ({ useWorkspaceStore: () => workspaceState }))
import EnterpriseCommunication from './EnterpriseCommunication.vue'
import { useUserStore } from '@/stores/user'

beforeEach(() => {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  sessionStorage.clear()
  workspaceState.activeSpace = 'enterprise'
  workspaceState.activeEnterpriseId = null
  workspaceState.activateEnterprise.mockClear()
  localStorage.removeItem('review_token')
  useUserStore().profile!.id = 1
  Object.values(mock).forEach(fn => fn.mockReset())
  mock.listMyEnterprises.mockResolvedValue([{ id: 10, name: '甲公司', role: 'owner', status: 'active' }])
  mock.listEnterpriseGroups.mockResolvedValue([])
  mock.listEnterpriseProjects.mockResolvedValue([])
  mock.listEnterpriseRemoteApprovals.mockResolvedValue({ items: [], total: 0 })
  mock.listEnterpriseDirectory.mockResolvedValue([{ user_id: 1, display_name: '甲' }, { user_id: 2, display_name: '乙' }])
  mock.listEnterpriseConversations.mockResolvedValue({ items: [], assistant_available: false })
  mock.openEnterpriseConversation.mockImplementation(async (id, input) => ({
    id: 9, enterprise_id: id, kind: input.kind, group_id: null, participant_low_id: null, participant_high_id: null,
  }))
  mock.getEnterpriseMessages.mockResolvedValue({ items: [], assistant_available: false })
  mock.getEnterpriseAssistantBillingContext.mockResolvedValue({ status: 'unassigned', group_id: null, group_name: null, enabled: false })
  mock.listEnterpriseAssistantRuns.mockResolvedValue({ items: [], assistant_mode: 'enterprise_agent_runtime' })
  mock.listEnterprisePosts.mockResolvedValue({ items: [] })
  mock.listEnterpriseAnnouncements.mockResolvedValue({ items: [] })
})
afterEach(() => {
  vi.unstubAllGlobals(); vi.useRealTimers()
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: initialViewport.width })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: initialViewport.height })
})

describe('企业沟通', () => {
  it.each([
    [40360, '小菱暂不可用'],
    [40361, '请联系管理员设置默认小组'],
  ])('归属状态 %s 时发送的聊天消息保留且不退出企业空间', async (code, message) => {
    vi.stubGlobal('crypto', { randomUUID: () => 'gated-message',
      subtle: { digest: async () => new Uint8Array([1, 2, 3]).buffer } })
    mock.getEnterpriseAssistantBillingContext.mockResolvedValue({
      status: 'unassigned', group_id: null, group_name: null, enabled: false,
    })
    mock.sendEnterpriseMessage.mockResolvedValue({
      message: { id: 201, conversation_id: 9, sender_id: 1, sender_type: 'human',
        content: '@小菱 总结讨论', client_message_id: 'gated-message' },
      assistant_available: true,
    })
    mock.requestEnterpriseAssistant.mockRejectedValue({ code,
      message: '企业会话计费归属正在验证，暂不能发起新的小菱任务' })
    const wrapper = mount(EnterpriseCommunication)
    try {
      await flushPromises()
      await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
      await flushPromises()
      await wrapper.get('#message-content').setValue('@小菱 总结讨论')
      await wrapper.get('.composer').trigger('submit')
      await flushPromises()
      expect(mock.sendEnterpriseMessage).toHaveBeenCalledTimes(1)
      expect(wrapper.text()).toContain('@小菱 总结讨论')
      expect(wrapper.text()).toContain(message)
      expect(wrapper.text()).toContain('甲公司')
      expect(wrapper.find('#message-content').exists()).toBe(true)
    } finally { wrapper.unmount() }
  })

  it('发起小菱遇到真实权限撤销仍清除企业会话', async () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'revoked-message',
      subtle: { digest: async () => new Uint8Array([1, 2, 3]).buffer } })
    mock.sendEnterpriseMessage.mockResolvedValue({
      message: { id: 202, conversation_id: 9, sender_id: 1, sender_type: 'human',
        content: '@小菱 不应继续显示', client_message_id: 'revoked-message' },
      assistant_available: true,
    })
    mock.requestEnterpriseAssistant.mockRejectedValue({ code: 40300, message: '企业访问权限已撤销' })
    const wrapper = mount(EnterpriseCommunication)
    try {
      await flushPromises()
      await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
      await flushPromises()
      await wrapper.get('#message-content').setValue('@小菱 不应继续显示')
      await wrapper.get('.composer').trigger('submit')
      await flushPromises()
      expect(wrapper.text()).toContain('企业内容权限已变化')
      expect(wrapper.text()).not.toContain('@小菱 不应继续显示')
    } finally { wrapper.unmount() }
  })

  it('已排队运行因归属门禁停止时显示可理解状态而非内部代码', async () => {
    mock.listEnterpriseAssistantRuns.mockResolvedValueOnce({ items: [{
      run_id: 'ear_gated', status: 'suppressed', assistant_mode: 'enterprise_agent_runtime',
      error: 'billing_attribution_unavailable', recoverable: false, pending: null,
      initiator_user_id: 1, source_message_id: 7, message: null,
    }], assistant_mode: 'enterprise_agent_runtime' })
    const wrapper = mount(EnterpriseCommunication)
    try {
      await flushPromises()
      await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
      await flushPromises()
      expect(wrapper.text()).toContain('小菱暂不可用')
      expect(wrapper.text()).not.toContain('billing_attribution_unavailable')
      expect(wrapper.text()).not.toContain('恢复运行')
    } finally { wrapper.unmount() }
  })

  it('恢复已有运行遇到归属门禁时保留会话和运行状态', async () => {
    mock.listEnterpriseAssistantRuns.mockResolvedValueOnce({ items: [{
      run_id: 'ear_retry_gated', status: 'retryable', assistant_mode: 'enterprise_agent_runtime',
      error: null, recoverable: true, pending: null, initiator_user_id: 1, source_message_id: 7, message: null,
    }], assistant_mode: 'enterprise_agent_runtime' })
    mock.retryEnterpriseAssistantRun.mockRejectedValueOnce({ code: 40360 })
    const wrapper = mount(EnterpriseCommunication)
    try {
      await flushPromises()
      await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
      await flushPromises()
      await wrapper.findAll('button').find(item => item.text() === '恢复运行')!.trigger('click')
      await flushPromises()
      expect(wrapper.text()).toContain('小菱暂不可用')
      expect(wrapper.find('#message-content').exists()).toBe(true)
      expect(wrapper.text()).toContain('运行中断，可恢复')
    } finally { wrapper.unmount() }
  })
  it('公司群只按服务端预览显示未启用的计费归属', async () => {
    mock.getEnterpriseAssistantBillingContext.mockResolvedValueOnce({ status: 'group', group_id: 11, group_name: '研发组', enabled: false })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    expect(mock.getEnterpriseAssistantBillingContext).toHaveBeenCalledWith(10, 9, null, expect.any(AbortSignal))
    expect(wrapper.get('.assistant-billing-context').text()).toContain('研发组')
    expect(wrapper.get('.assistant-billing-context').text()).toContain('尚未启用')
    wrapper.unmount()
  })

  it('前台恢复时组织列表卡住也能独立刷新计费归属', async () => {
    mock.getEnterpriseAssistantBillingContext
      .mockResolvedValueOnce({ status: 'group', group_id: 11, group_name: '原小组', enabled: true })
      .mockResolvedValueOnce({ status: 'group', group_id: 12, group_name: '新小组', enabled: true })
    const wrapper = mount(EnterpriseCommunication)
    try {
      await flushPromises()
      await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
      await flushPromises()
      expect(wrapper.get('.assistant-billing-context').text()).toContain('原小组')
      mock.listEnterpriseGroups.mockReturnValueOnce(new Promise(() => {}))
      window.dispatchEvent(new Event('focus'))
      await flushPromises()
      expect(wrapper.get('.assistant-billing-context').text()).toContain('新小组')
    } finally { wrapper.unmount() }
  })

  it('重复前台刷新时取消旧计费请求，迟到响应不会覆盖新结果', async () => {
    let resolveOld!: (value: unknown) => void
    let resolveNew!: (value: unknown) => void
    const signals: AbortSignal[] = []
    mock.getEnterpriseAssistantBillingContext.mockImplementation((_enterprise, _room, _token, signal: AbortSignal) => {
      signals.push(signal)
      return new Promise(resolve => { if (signals.length === 1) resolveOld = resolve; else resolveNew = resolve })
    })
    const wrapper = mount(EnterpriseCommunication)
    try {
      await flushPromises()
      await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
      await flushPromises()
      window.dispatchEvent(new Event('focus'))
      await flushPromises()
      expect(signals).toHaveLength(2)
      expect(signals[0].aborted).toBe(true)
      expect(signals[1].aborted).toBe(false)
      resolveNew({ status: 'group', group_id: 12, group_name: '新小组', enabled: true })
      await flushPromises()
      resolveOld({ status: 'group', group_id: 11, group_name: '旧小组', enabled: true })
      await flushPromises()
      expect(wrapper.get('.assistant-billing-context').text()).toContain('新小组')
      expect(wrapper.text()).not.toContain('旧小组')
    } finally { wrapper.unmount() }
  })

  it('离开企业聊天时取消仍在等待的计费预览', async () => {
    let signal!: AbortSignal
    mock.getEnterpriseAssistantBillingContext.mockImplementation((_enterprise, _room, _token, current: AbortSignal) => {
      signal = current
      return new Promise(() => {})
    })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    expect(signal).toBeInstanceOf(AbortSignal)
    wrapper.unmount()
    expect(signal.aborted).toBe(true)
  })

  it('计费归属读取超时后可重试，不影响会话消息', async () => {
    mock.getEnterpriseAssistantBillingContext.mockRejectedValueOnce({ code: 'ECONNABORTED' })
      .mockResolvedValueOnce({ status: 'unassigned', group_id: null, group_name: null, enabled: false })
    const wrapper = mount(EnterpriseCommunication)
    try {
      await flushPromises()
      await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
      await flushPromises()
      expect(wrapper.get('.assistant-billing-context').text()).toContain('无法读取')
      expect(wrapper.find('#message-content').exists()).toBe(true)
      await wrapper.get('[data-action="retry-billing-context"]').trigger('click')
      await flushPromises()
      expect(wrapper.get('.assistant-billing-context').text()).toContain('未分组')
    } finally { wrapper.unmount() }
  })

  it('缺少默认组明确提示管理员设置，失败时不显示旧组且允许重试', async () => {
    mock.getEnterpriseAssistantBillingContext.mockResolvedValueOnce({ status: 'default_group_required', group_id: null, group_name: null, enabled: true })
      .mockRejectedValueOnce({ code: 403 })
      .mockResolvedValueOnce({ status: 'unassigned', group_id: null, group_name: null, enabled: true })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    expect(wrapper.get('.assistant-billing-context').text()).toContain('联系管理员设置默认小组')
    window.dispatchEvent(new Event('focus'))
    await flushPromises()
    expect(wrapper.get('.assistant-billing-context').text()).toContain('无法读取')
    await wrapper.get('[data-action="retry-billing-context"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('.assistant-billing-context').text()).toContain('未分组')
    wrapper.unmount()
  })

  it('小组群不请求公司群和私聊的计费预览', async () => {
    mock.listEnterpriseGroups.mockResolvedValue([{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'member' }])
    mock.openEnterpriseConversation.mockResolvedValue({ id: 20, enterprise_id: 10, kind: 'group', group_id: 22 })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '研发组')!.trigger('click')
    await flushPromises()
    expect(mock.getEnterpriseAssistantBillingContext).not.toHaveBeenCalled()
    expect(wrapper.find('.assistant-billing-context').exists()).toBe(false)
    wrapper.unmount()
  })

  it('私聊预览使用该会话，并忽略旧公司群迟到的归属', async () => {
    let resolveOld!: (value: unknown) => void
    mock.openEnterpriseConversation.mockImplementation(async (id, input) => ({
      id: input.kind === 'direct' ? 20 : 9, enterprise_id: id, kind: input.kind, group_id: null,
      participant_low_id: 1, participant_high_id: 2,
    }))
    mock.getEnterpriseAssistantBillingContext.mockImplementation((_enterprise, conversation) =>
      conversation === 9 ? new Promise(resolve => { resolveOld = resolve })
        : Promise.resolve({ status: 'group', group_id: 12, group_name: '新小组', enabled: true }))
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    await wrapper.get('#direct-member').setValue('2')
    await wrapper.get('.direct-start').trigger('submit')
    await flushPromises()
    expect(mock.getEnterpriseAssistantBillingContext).toHaveBeenLastCalledWith(10, 20, null, expect.any(AbortSignal))
    expect(wrapper.get('.assistant-billing-context').text()).toContain('新小组')
    resolveOld({ status: 'group', group_id: 11, group_name: '旧小组', enabled: true })
    await flushPromises()
    expect(wrapper.get('.assistant-billing-context').text()).toContain('新小组')
    expect(wrapper.text()).not.toContain('旧小组')
    wrapper.unmount()
  })

  it('跨标签页凭据变化清除归属，旧响应不能重现', async () => {
    localStorage.setItem('review_token', 'account-a')
    let resolveOld!: (value: unknown) => void
    mock.getEnterpriseAssistantBillingContext.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve }))
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    localStorage.setItem('review_token', 'account-b')
    window.dispatchEvent(new StorageEvent('storage', { key: 'review_token', oldValue: 'account-a', newValue: 'account-b' }))
    resolveOld({ status: 'group', group_id: 11, group_name: '旧账号小组', enabled: true })
    await flushPromises()
    expect(wrapper.text()).not.toContain('旧账号小组')
    expect(wrapper.find('.assistant-billing-context').exists()).toBe(false)
    wrapper.unmount()
  })

  it('旧账号请求完成不能清除新账号已经读取的归属', async () => {
    localStorage.setItem('review_token', 'account-a')
    let resolveOld!: (value: unknown) => void
    mock.listMyEnterprises.mockResolvedValueOnce([{ id: 10, name: '旧企业', role: 'member', status: 'active' }])
      .mockResolvedValueOnce([{ id: 20, name: '新企业', role: 'member', status: 'active' }])
    mock.openEnterpriseConversation.mockImplementation(async (id, input) => ({
      id: id === 10 ? 9 : 29, enterprise_id: id, kind: input.kind, group_id: null,
    }))
    mock.getEnterpriseAssistantBillingContext.mockImplementation((enterprise) =>
      enterprise === 10 ? new Promise(resolve => { resolveOld = resolve })
        : Promise.resolve({ status: 'group', group_id: 22, group_name: '新账号小组', enabled: true }))
    const wrapper = mount(EnterpriseCommunication)
    try {
      await flushPromises()
      await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
      await flushPromises()
      localStorage.setItem('review_token', 'account-b')
      useUserStore().profile!.id = 2
      await flushPromises()
      await vi.waitFor(() => {
        expect(wrapper.text()).toContain('新企业')
        expect(wrapper.findAll('button').some(item => item.text() === '公司群')).toBe(true)
      })
      await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
      await flushPromises()
      expect(mock.getEnterpriseAssistantBillingContext).toHaveBeenLastCalledWith(20, 29, 'account-b', expect.any(AbortSignal))
      expect(wrapper.get('.assistant-billing-context').text()).toContain('新账号小组')
      resolveOld({ status: 'group', group_id: 11, group_name: '旧账号小组', enabled: true })
      await flushPromises()
      expect(wrapper.get('.assistant-billing-context').text()).toContain('新账号小组')
    } finally {
      wrapper.unmount()
    }
  })

  it('服务端返回不一致数据时不猜测计费归属', async () => {
    mock.getEnterpriseAssistantBillingContext.mockResolvedValueOnce({ status: 'group', group_id: null, group_name: '无效小组', enabled: true })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    expect(wrapper.get('.assistant-billing-context').text()).toContain('无法读取')
    expect(wrapper.text()).not.toContain('无效小组')
    wrapper.unmount()
  })

  it('同一企业和会话切换账号后，旧预览不能覆盖新账号归属', async () => {
    localStorage.setItem('review_token', 'account-a')
    let resolveOld!: (value: unknown) => void
    mock.getEnterpriseAssistantBillingContext.mockImplementation((_enterprise, _room, token) =>
      token === 'account-a' ? new Promise(resolve => { resolveOld = resolve })
        : Promise.resolve({ status: 'group', group_id: 12, group_name: '乙的默认组', enabled: true }))
    const wrapper = mount(EnterpriseCommunication)
    try {
      await flushPromises()
      await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
      await flushPromises()
      localStorage.setItem('review_token', 'account-b')
      useUserStore().profile!.id = 2
      await flushPromises()
      await vi.waitFor(() => expect(wrapper.findAll('button').some(item => item.text() === '公司群')).toBe(true))
      await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
      await flushPromises()
      expect(wrapper.get('.assistant-billing-context').text()).toContain('乙的默认组')
      resolveOld({ status: 'group', group_id: 11, group_name: '甲的默认组', enabled: true })
      await flushPromises()
      expect(wrapper.get('.assistant-billing-context').text()).toContain('乙的默认组')
      expect(wrapper.text()).not.toContain('甲的默认组')
    } finally { wrapper.unmount() }
  })

  it('主动讨论设置仅企业管理员可见，切换企业时关闭', async () => {
    mock.listMyEnterprises.mockResolvedValue([
      { id: 10, name: '甲公司', role: 'admin', status: 'active' },
      { id: 20, name: '乙公司', role: 'member', status: 'active' },
    ])
    const wrapper = mount(EnterpriseCommunication, { global: { stubs: { EnterpriseProactiveSettings: true } } })
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '小菱主动讨论设置')!.trigger('click')
    expect(wrapper.find('enterprise-proactive-settings-stub').exists()).toBe(true)
    await wrapper.get('#company-select').setValue('20'); await flushPromises()
    expect(wrapper.text()).not.toContain('小菱主动讨论设置')
    expect(wrapper.find('enterprise-proactive-settings-stub').exists()).toBe(false)
    await wrapper.get('#company-select').setValue('10'); await flushPromises()
    expect(wrapper.find('enterprise-proactive-settings-stub').exists()).toBe(false)
    wrapper.unmount()
  })
  it('企业资产有未保存修改时，切换功能必须先确认', async () => {
    const mayLeave = vi.fn(() => false)
    const assetStub = defineComponent({ setup(_props, { expose }) {
      expose({ mayLeave }); return () => h('div', { 'data-test': 'asset-editor' }, '未保存')
    } })
    const wrapper = mount(EnterpriseCommunication, { global: { stubs: { EnterpriseAssets: assetStub } } })
    await flushPromises()
    await wrapper.findAll('.workspace-tabs button').find(item => item.text() === 'Agent 与 Skill')!.trigger('click')
    expect(wrapper.find('[data-test="asset-editor"]').exists()).toBe(true)
    await wrapper.findAll('.workspace-tabs button').find(item => item.text() === '企业论坛')!.trigger('click')
    expect(mayLeave).toHaveBeenCalled()
    expect(wrapper.find('[data-test="asset-editor"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('企业切换会直接卸载旧企业资产，不受离开提示阻拦', async () => {
    mock.listMyEnterprises.mockResolvedValue([
      { id: 10, name: '甲公司', role: 'owner', status: 'active' },
      { id: 20, name: '乙公司', role: 'member', status: 'active' },
    ])
    const mayLeave = vi.fn(() => false)
    const assetStub = defineComponent({ setup(_props, { expose }) {
      expose({ mayLeave }); return () => h('div', { 'data-test': 'asset-editor' }, '旧企业内容')
    } })
    const wrapper = mount(EnterpriseCommunication, { global: { stubs: { EnterpriseAssets: assetStub } } })
    await flushPromises()
    await wrapper.findAll('.workspace-tabs button').find(item => item.text() === 'Agent 与 Skill')!.trigger('click')
    await wrapper.get('#company-select').setValue('20'); await flushPromises()
    expect(mayLeave).not.toHaveBeenCalled()
    expect(wrapper.find('[data-test="asset-editor"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('旧企业内容')
    wrapper.unmount()
  })
  it('普通企业成员可进入远测申请与审批页，但不获得组织管理入口', async () => {
    mock.listMyEnterprises.mockResolvedValue([{ id: 10, name: '甲公司', role: 'member', status: 'active' }])
    mock.listEnterpriseProjects.mockResolvedValue([{
      project_id: 500, project_name: '甲组代码审查', enterprise_id: 10, group_id: 22,
      group_name: '研发组', role: 'reviewer',
    }])
    mock.listEnterpriseRemoteApprovals.mockResolvedValue({ items: [], total: 0 })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()

    const entry = wrapper.findAll('.workspace-tabs button').find(item => item.text() === '远测申请与审批')
    expect(entry).toBeDefined()
    await entry!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('提交远测申请')
    expect(wrapper.text()).toContain('甲组代码审查')
    expect(wrapper.text()).not.toContain('组织管理')
    expect(mock.listEnterpriseProjects).toHaveBeenCalledWith(10)
    expect(mock.listEnterpriseRemoteApprovals).toHaveBeenCalledWith(10, 20, 0)
    wrapper.unmount()
  })

  it('恢复企业选择前先核对服务端成员列表，不再属于该企业时回退到有效企业', async () => {
    workspaceState.activeEnterpriseId = 99
    mock.listMyEnterprises.mockResolvedValue([
      { id: 10, name: '甲公司', role: 'owner', status: 'active' },
      { id: 11, name: '乙公司', role: 'member', status: 'active' },
    ])
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()

    expect(mock.listEnterpriseGroups).toHaveBeenCalledWith(10)
    expect(workspaceState.activateEnterprise).toHaveBeenCalledWith(10)
    wrapper.unmount()
  })

  it('恢复有效企业选择时保持用户上次选中的企业', async () => {
    workspaceState.activeEnterpriseId = 11
    mock.listMyEnterprises.mockResolvedValue([
      { id: 10, name: '甲公司', role: 'owner', status: 'active' },
      { id: 11, name: '乙公司', role: 'member', status: 'active' },
    ])
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()

    expect(mock.listEnterpriseGroups).toHaveBeenCalledWith(11)
    wrapper.unmount()
  })

  it('会话列表标出企业订阅状态和当前席位占用', async () => {
    mock.listMyEnterprises.mockResolvedValue([{
      id: 10, name: '甲公司', role: 'owner', status: 'active', seat_limit: 4,
      active_member_count: 3, subscription_status: 'grace', subscription_ends_at: '2026-11-01T00:00:00',
      over_capacity: false,
    }])
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    expect(wrapper.text()).toContain('续费宽限期')
    expect(wrapper.text()).toContain('席位 3/4')
    wrapper.unmount()
  })

  it('会话搜索无结果时提供清除搜索操作', async () => {
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    const search = wrapper.get('.conversation-search input')
    await search.setValue('不存在的会话')
    expect(wrapper.get('.conversation-empty').text()).toBe('没有匹配的会话，清除搜索')
    await wrapper.get('.conversation-empty').trigger('click')
    expect((wrapper.get('.conversation-search input').element as HTMLInputElement).value).toBe('')
    expect(wrapper.get('.conversation-empty').text()).toContain('打开公司群')
    expect(mock.openEnterpriseConversation).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('群聊标题说明真实会话范围，不把通讯录分页数量当作可见人数', async () => {
    mock.listEnterpriseGroups.mockResolvedValue([{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'leader' }])
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('.conversation-quick-actions button').find(item => item.text().includes('研发组'))!.trigger('click')
    await flushPromises()
    expect(wrapper.get('.chat-title-block').text()).toContain('本小组成员可见')
    expect(wrapper.get('.chat-title-block').text()).not.toContain('企业成员可见')
    expect(wrapper.get('.chat-title-block').text()).not.toContain('2 位')
    wrapper.unmount()
  })

  it('刷新权限后从最近会话移除已撤销的小组，并保留公司群', async () => {
    mock.listEnterpriseGroups.mockResolvedValue([{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'member' }])
    mock.listEnterpriseConversations.mockResolvedValue({ items: [
      { id: 9, enterprise_id: 10, kind: 'company', group_id: null },
      { id: 20, enterprise_id: 10, kind: 'group', group_id: 22 },
    ], assistant_available: false })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    expect(wrapper.findAll('.room-row').some(item => item.text().includes('研发组'))).toBe(true)
    await wrapper.findAll('.room-row').find(item => item.text().includes('研发组'))!.trigger('click')
    await flushPromises()

    mock.listEnterpriseGroups.mockResolvedValue([])
    await wrapper.findAll('button').find(item => item.text() === '刷新企业')!.trigger('click')
    await flushPromises()

    expect(wrapper.findAll('.room-row').some(item => item.text().includes('研发组'))).toBe(false)
    expect(wrapper.findAll('.room-row').some(item => item.text().includes('公司群'))).toBe(true)
    expect(wrapper.find('.chat-welcome').exists()).toBe(true)
    wrapper.unmount()
  })

  it('管理入口仅对企业管理员和所属小组长显示', async () => {
    mock.listMyEnterprises.mockResolvedValue([{ id: 10, name: '甲公司', role: 'member', status: 'active' }])
    mock.listEnterpriseGroups.mockResolvedValue([{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'leader' }])
    const wrapper = mount(EnterpriseCommunication); await flushPromises()
    expect(wrapper.text()).toContain('组织管理')
    expect(wrapper.text()).toContain('企业用量')
    wrapper.unmount()
    mock.listEnterpriseGroups.mockResolvedValue([{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'member' }])
    const ordinary = mount(EnterpriseCommunication); await flushPromises()
    expect(ordinary.text()).not.toContain('组织管理')
    expect(ordinary.text()).not.toContain('企业用量')
    ordinary.unmount()
  })

  it('企业管理员可管理组织但不会从聊天列表进入未加入的小组会话', async () => {
    mock.listMyEnterprises.mockResolvedValue([{ id: 10, name: '甲公司', role: 'admin', status: 'active' }])
    mock.listEnterpriseGroups.mockResolvedValue([{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: null }])
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    expect(wrapper.text()).toContain('组织管理')
    expect(wrapper.findAll('.conversation-quick-actions button').some(button => button.text().includes('研发组'))).toBe(false)
    wrapper.unmount()
  })

  it('离开组织管理时刷新公司角色，降权后退出管理和统计页面', async () => {
    const wrapper = mount(EnterpriseCommunication, { global: { stubs: { EnterpriseManagement: true, EnterpriseUsage: true } } })
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '组织管理')!.trigger('click')
    mock.listMyEnterprises.mockResolvedValue([{ id: 10, name: '甲公司', role: 'member', status: 'active' }])
    mock.listEnterpriseGroups.mockResolvedValue([])
    await wrapper.findAll('button').find(item => item.text() === '企业用量')!.trigger('click'); await flushPromises()
    expect(wrapper.text()).not.toContain('组织管理')
    expect(wrapper.text()).not.toContain('企业用量')
    expect(wrapper.text()).toContain('选择一个会话')
    wrapper.unmount()
  })

  it('刷新企业会清理已撤销的小组长入口，同时保留无关会话草稿', async () => {
    mock.listMyEnterprises.mockResolvedValue([{ id: 10, name: '甲公司', role: 'member', status: 'active' }])
    mock.listEnterpriseGroups.mockResolvedValue([{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'leader' }])
    const wrapper = mount(EnterpriseCommunication, { global: { stubs: { EnterpriseUsage: true } } }); await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '企业用量')!.trigger('click')
    mock.listEnterpriseGroups.mockResolvedValue([{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'member' }])
    await wrapper.findAll('button').find(item => item.text() === '刷新企业')!.trigger('click'); await flushPromises()
    expect(wrapper.text()).not.toContain('企业用量')
    expect(wrapper.text()).toContain('选择一个会话')
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    await wrapper.get('#message-content').setValue('公司群草稿')
    await wrapper.findAll('button').find(item => item.text() === '刷新企业')!.trigger('click'); await flushPromises()
    expect((wrapper.get('#message-content').element as HTMLTextAreaElement).value).toBe('公司群草稿')
    wrapper.unmount()
  })

  it('刷新企业权限后移除已撤销小组的旧会话入口', async () => {
    mock.listEnterpriseGroups.mockResolvedValue([{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'member' }])
    mock.listEnterpriseConversations.mockResolvedValue({ items: [
      { id: 9, enterprise_id: 10, kind: 'company', group_id: null },
      { id: 20, enterprise_id: 10, kind: 'group', group_id: 22 },
    ], assistant_available: false })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    expect(wrapper.findAll('.room-row').some(item => item.text().includes('研发组'))).toBe(true)
    await wrapper.findAll('.room-row').find(item => item.text().includes('研发组'))!.trigger('click')
    await flushPromises()

    mock.listEnterpriseGroups.mockResolvedValue([])
    await wrapper.findAll('button').find(item => item.text() === '刷新企业')!.trigger('click')
    await flushPromises()

    expect(wrapper.findAll('.room-row').some(item => item.text().includes('研发组'))).toBe(false)
    expect(wrapper.findAll('.room-row').some(item => item.text().includes('公司群'))).toBe(true)
    expect(wrapper.find('.chat-welcome').exists()).toBe(true)
    wrapper.unmount()
  })

  it('组织信息并发刷新时不会用迟到的旧数据覆盖最新结果', async () => {
    const wrapper = mount(EnterpriseCommunication, { global: { stubs: { EnterpriseManagement: true, EnterpriseUsage: true } } })
    await flushPromises()
    let resolveOld!: (value: unknown) => void
    mock.listEnterpriseGroups.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve }))
      .mockResolvedValueOnce([{ id: 2, enterprise_id: 10, name: '新小组', status: 'active', my_role: 'leader' }])
    await wrapper.findAll('button').find(item => item.text() === '组织管理')!.trigger('click')
    await wrapper.findAll('button').find(item => item.text() === '聊天')!.trigger('click')
    await wrapper.findAll('button').find(item => item.text() === '组织管理')!.trigger('click')
    await wrapper.findAll('button').find(item => item.text() === '聊天')!.trigger('click'); await flushPromises()
    resolveOld([{ id: 1, enterprise_id: 10, name: '旧小组', status: 'active', my_role: 'leader' }]); await flushPromises()
    expect(wrapper.text()).toContain('新小组')
    expect(wrapper.text()).not.toContain('旧小组')
    wrapper.unmount()
  })
  it('页面重新获得焦点时复核企业权限，并清除已撤销小组的消息', async () => {
    const group = { id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'leader' }
    mock.listEnterpriseGroups.mockResolvedValueOnce([group]).mockResolvedValueOnce([])
    mock.openEnterpriseConversation.mockImplementation(async (id, input) => ({
      id: 20, enterprise_id: id, kind: input.kind, group_id: input.group_id ?? null,
    }))
    mock.getEnterpriseMessages.mockResolvedValue({ items: [
      { id: 1, conversation_id: 20, sender_id: 2, content: '撤权前的群聊内容' },
    ], assistant_available: false })
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })

    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '研发组')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('撤权前的群聊内容')

    window.dispatchEvent(new Event('focus'))
    await flushPromises()

    expect(mock.listEnterpriseGroups).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('小组会话权限已变化')
    expect(wrapper.text()).not.toContain('撤权前的群聊内容')
    wrapper.unmount()
  })
  it('隐藏标签页暂停聊天轮询，重新显示后立即同步', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    const messageReads = mock.getEnterpriseMessages.mock.calls.length

    await vi.advanceTimersByTimeAsync(5000)
    expect(mock.getEnterpriseMessages).toHaveBeenCalledTimes(messageReads)

    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(mock.getEnterpriseMessages).toHaveBeenCalledTimes(messageReads + 1)
    wrapper.unmount()
  })
  it('聊天加载最新消息，保留历史分页同时持续接收新消息', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    const msg = (id: number) => ({ id, conversation_id: 9, sender_id: 1, content: `消息${id}` })
    mock.getEnterpriseMessages.mockImplementation(async (_enterprise, _room, after, before) => {
      if (before) return { items: Array.from({ length: 5 }, (_, i) => msg(i + 1)), assistant_available: false }
      if (after === 105) return { items: [msg(106)], assistant_available: false }
      return { items: Array.from({ length: 100 }, (_, i) => msg(i + 6)), assistant_available: false }
    })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('消息105')
    expect(wrapper.text()).not.toContain('消息1消息')
    expect(wrapper.text()).toContain('加载更多消息')
    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(mock.getEnterpriseMessages).toHaveBeenLastCalledWith(10, 9, 105)
    expect(wrapper.text()).toContain('消息106')
    expect(wrapper.text()).toContain('加载更多消息')
    await wrapper.findAll('button').find(item => item.text() === '加载更多消息')!.trigger('click')
    await flushPromises()
    expect(mock.getEnterpriseMessages).toHaveBeenLastCalledWith(10, 9, undefined, 6)
    expect(wrapper.findAll('.message p').map(item => item.text())).toEqual(
      Array.from({ length: 106 }, (_, i) => `消息${i + 1}`),
    )
    expect(wrapper.text()).not.toContain('加载更多消息')
    wrapper.unmount()
  })
  it('小菱可用时不显示重复说明，不可用时提供简洁状态', async () => {
    mock.getEnterpriseMessages.mockResolvedValue({ items: [], assistant_available: true })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    expect(wrapper.find('.assistant-status-note').exists()).toBe(false)
    mock.getEnterpriseMessages.mockResolvedValue({ items: [], assistant_available: false })
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    expect(wrapper.get('.assistant-status-note').attributes('role')).toBe('status')
    expect(wrapper.get('.assistant-status-note').text()).toContain('小菱暂不可用')
    wrapper.unmount()
  })
  it('打开公司群，普通消息只落聊天；艾特小菱时显示企业运行状态和答复', async () => {
    vi.stubGlobal('crypto', { subtle: undefined })
    mock.sendEnterpriseMessage.mockImplementation(async (_enterprise, conversation, content, clientId) => ({
      message: { id: 4, conversation_id: conversation, sender_id: 1, content, client_message_id: clientId },
      assistant_available: false,
    }))
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    expect(wrapper.text()).toContain('甲公司')
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    expect(mock.openEnterpriseConversation).toHaveBeenCalledWith(10, { kind: 'company' })
    expect(wrapper.text()).toContain('小菱暂不可用')
    await wrapper.get('#message-content').setValue('团队讨论')
    await wrapper.get('.composer').trigger('submit')
    await flushPromises()
    expect(mock.sendEnterpriseMessage).toHaveBeenCalledWith(10, 9, '团队讨论', expect.any(String), null)
    expect(wrapper.text()).toContain('团队讨论')

    mock.sendEnterpriseMessage.mockResolvedValueOnce({
      message: { id: 5, conversation_id: 9, sender_id: 1, sender_type: 'human',
        content: '@小菱 帮我解释这段讨论', client_message_id: 'ask-1' },
      assistant_available: true,
    })
    mock.requestEnterpriseAssistant.mockResolvedValueOnce({
      run_id: 'ear_test', status: 'completed', assistant_mode: 'enterprise_agent_runtime',
      message: { id: 6, conversation_id: 9, sender_id: null, sender_type: 'assistant', assistant_run_id: 1,
        content: '根据当前会话，大家在讨论发布顺序。', client_message_id: 'ear_test' },
    })
    await wrapper.get('#message-content').setValue('@小菱 帮我解释这段讨论')
    await wrapper.get('.composer').trigger('submit')
    await flushPromises()
    expect(mock.requestEnterpriseAssistant).toHaveBeenCalledWith(10, 9, 5, null)
    expect(wrapper.text()).toContain('小菱 · 企业助手')
    expect(wrapper.text()).toContain('大家在讨论发布顺序')
    wrapper.unmount()
  })

  it('在悬浮会话内核对权限意见，再由原发起人继续模型发送', async () => {
    const waiting = {
      run_id: 'ear_clarification', status: 'awaiting_clarification', assistant_mode: 'enterprise_agent_runtime',
      message: null, pending: { kind: 'clarification', call_id: 'model-send:ear_clarification', operation: '发送本次小菱回复',
        approval_id: 77, question: null, preview: { request_id: 77, action: 'send_enterprise_assistant_response' } },
      recoverable: false, initiator_user_id: 1, source_message_id: 61,
    }
    const preview = {
      request_id: 77, version: 1, call_id: 'model-send:ear_clarification', action: 'send_enterprise_assistant_response',
      title: null, status: 'waiting', can_resume: false,
      candidates: [{ message_id: 60, author_id: 2, rank: 4, content: '@小菱 暂停创建这个团队', decision: 'pending', can_decide: true }],
    }
    mock.listEnterpriseAssistantRuns.mockResolvedValueOnce({ items: [waiting], assistant_mode: 'enterprise_agent_runtime' })
    mock.getEnterpriseAssistantClarification.mockResolvedValueOnce(preview)
    mock.decideEnterpriseAssistantClarification.mockResolvedValueOnce({ request_id: 77, status: 'complete' })
    mock.getEnterpriseAssistantRun.mockResolvedValueOnce(waiting)
    mock.getEnterpriseAssistantClarification.mockResolvedValueOnce({
      ...preview, status: 'complete', can_resume: true,
      candidates: [{ ...preview.candidates[0], decision: 'allow' }],
    })
    mock.resumeEnterpriseAssistantClarification.mockResolvedValueOnce({
      run_id: 'ear_clarification', status: 'completed', assistant_mode: 'enterprise_agent_runtime',
      message: { id: 99, conversation_id: 9, sender_id: null, sender_type: 'assistant',
        content: '模型回复已生成。', client_message_id: 'ear_clarification' },
      pending: null, recoverable: false, initiator_user_id: 1, source_message_id: 61,
    })

    const wrapper = mount(EnterpriseCommunication)
    try {
      await flushPromises()
      await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
      await flushPromises()
      expect(wrapper.text()).toContain('等待权限确认')
      expect(wrapper.text()).toContain('发送本次小菱回复')
      expect(wrapper.text()).toContain('@小菱 暂停创建这个团队')
      await wrapper.get('button[aria-label="弹出悬浮聊天窗口"]').trigger('click')
      await flushPromises()
      const floating = document.body.querySelector('.chat-pane.is-floating .assistant-clarification')
      expect(floating).not.toBeNull()

      const allowButton = [...floating!.querySelectorAll<HTMLButtonElement>('button')]
        .find(item => item.textContent?.trim() === '允许本次')
      allowButton!.click()
      await flushPromises()
      const continueButton = [...document.body.querySelectorAll<HTMLButtonElement>('.clarification-actions button')]
        .find(item => item.textContent?.trim() === '继续执行')
      expect(continueButton).toBeDefined()

      continueButton!.click()
      await flushPromises()
      expect(mock.resumeEnterpriseAssistantClarification).toHaveBeenCalledWith(10, 9, 'ear_clarification', 77, null)
      expect(mock.retryEnterpriseAssistantRun).not.toHaveBeenCalled()
      expect(document.body.textContent).toContain('模型回复已生成。')
    } finally { wrapper.unmount() }
  })

  it('小菱失败后可对原消息重试，不会再次发送聊天消息', async () => {
    vi.stubGlobal('crypto', {
      randomUUID: () => 'message-retry-key',
      subtle: { digest: async () => new Uint8Array([1, 2, 3]).buffer },
    })
    mock.sendEnterpriseMessage.mockResolvedValue({
      message: { id: 51, conversation_id: 9, sender_id: 1, sender_type: 'human',
        content: '@小菱 总结讨论', client_message_id: 'ask-retry' },
      assistant_available: true,
    })
    mock.requestEnterpriseAssistant.mockRejectedValueOnce({ code: 40200 })
      .mockResolvedValueOnce({ run_id: 'ear_retry', status: 'completed', assistant_mode: 'enterprise_agent_runtime',
        message: { id: 52, conversation_id: 9, sender_id: null, sender_type: 'assistant',
          content: '讨论已总结。', client_message_id: 'ear_retry' } })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    await wrapper.get('#message-content').setValue('@小菱 总结讨论')
    await wrapper.get('.composer').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('未确认的小菱请求')
    await wrapper.findAll('button').find(item => item.text() === '检查后重试小菱')!.trigger('click')
    await flushPromises()
    expect(mock.sendEnterpriseMessage).toHaveBeenCalledTimes(1)
    expect(mock.requestEnterpriseAssistant).toHaveBeenCalledTimes(2)
    expect(mock.requestEnterpriseAssistant).toHaveBeenLastCalledWith(10, 9, 51, null)
    expect(wrapper.text()).toContain('讨论已总结。')
    wrapper.unmount()
  })

  it('重新打开会话会恢复服务端待补充任务，并用 call_id 继续运行', async () => {
    mock.listEnterpriseAssistantRuns.mockResolvedValueOnce({ items: [{
      run_id: 'ear_input', status: 'waiting_input', assistant_mode: 'enterprise_agent_runtime',
      message: { id: 61, conversation_id: 9, sender_id: null, sender_type: 'assistant',
        content: '小菱需要补充信息。', client_message_id: 'ear_input' },
      pending: { kind: 'input', call_id: 'call_input_1', operation: 'ask_user', approval_id: null,
        question: '请确认目标版本。' },
      recoverable: false, initiator_user_id: 1, source_message_id: 60,
    }] })
    mock.answerEnterpriseAssistantRun.mockResolvedValueOnce({
      run_id: 'ear_input', status: 'completed', assistant_mode: 'enterprise_agent_runtime',
      message: { id: 61, conversation_id: 9, sender_id: null, sender_type: 'assistant',
        content: '已按目标版本完成。', client_message_id: 'ear_input' },
      pending: null, recoverable: false, initiator_user_id: 1, source_message_id: 60,
    })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('等待你补充信息')
    expect(wrapper.text()).toContain('请确认目标版本。')
    await wrapper.get('#assistant-answer').setValue('使用 v4.1')
    await wrapper.get('.assistant-answer').trigger('submit')
    await flushPromises()
    expect(mock.answerEnterpriseAssistantRun).toHaveBeenCalledWith(10, 9, 'ear_input', 'call_input_1', '使用 v4.1', null)
    expect(wrapper.text()).toContain('已按目标版本完成。')
    wrapper.unmount()
  })

  it('刷新后可停止活动的小菱运行，并由服务端确认停止状态', async () => {
    mock.listEnterpriseAssistantRuns.mockResolvedValueOnce({ items: [{
      run_id: 'ear_running', status: 'running', assistant_mode: 'enterprise_agent_runtime',
      message: { id: 62, conversation_id: 9, sender_id: null, sender_type: 'assistant',
        content: '小菱正在处理。', client_message_id: 'ear_running' },
      pending: null, recoverable: false, initiator_user_id: 1, source_message_id: 60,
    }] })
    mock.cancelEnterpriseAssistantRun.mockResolvedValueOnce({
      run_id: 'ear_running', status: 'cancelled', assistant_mode: 'enterprise_agent_runtime',
      message: { id: 62, conversation_id: 9, sender_id: null, sender_type: 'assistant',
        content: '本次企业小菱任务已停止。', client_message_id: 'ear_running' },
      pending: null, error: 'provider_attempt_pending_reconciliation', recoverable: false,
      initiator_user_id: 1, source_message_id: 60,
    })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('小菱正在处理')
    await wrapper.findAll('button').find(item => item.text() === '停止本次运行')!.trigger('click')
    await flushPromises()
    expect(mock.cancelEnterpriseAssistantRun).toHaveBeenCalledWith(10, 9, 'ear_running', '会话成员主动停止', null)
    expect(wrapper.text()).toContain('运行已停止')
    expect(wrapper.text()).toContain('后续不会自动续跑')
    expect(wrapper.text()).not.toContain('运行已暂停')
    wrapper.unmount()
  })

  it('待核对运行不暴露内部错误码也不显示安全恢复按钮', async () => {
    mock.listEnterpriseAssistantRuns.mockResolvedValueOnce({ items: [{
      run_id: 'ear_reconcile', status: 'awaiting_reconciliation', assistant_mode: 'enterprise_agent_runtime',
      message: { id: 63, conversation_id: 9, sender_id: null, sender_type: 'assistant',
        content: '小菱请求状态待核对。', client_message_id: 'ear_reconcile' },
      pending: null, error: 'provider_attempt_pending_reconciliation', recoverable: false,
      initiator_user_id: 1, source_message_id: 60,
    }] })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('模型请求状态待核对')
    expect(wrapper.text()).toContain('为避免重复调用，运行已暂停')
    expect(wrapper.text()).toContain('请联系平台管理员核对')
    expect(wrapper.text()).not.toContain('provider_attempt_pending_reconciliation')
    expect(wrapper.findAll('button').some(item => item.text() === '恢复运行')).toBe(false)
    wrapper.unmount()
  })

  it('平台双人确认未发送后允许用户显式恢复', async () => {
    mock.listEnterpriseAssistantRuns.mockResolvedValueOnce({ items: [{
      run_id: 'ear_released', status: 'retryable', assistant_mode: 'enterprise_agent_runtime',
      message: { id: 64, conversation_id: 9, sender_id: null, sender_type: 'assistant',
        content: '小菱请求已停止，等待人工核对。', client_message_id: 'ear_released' },
      pending: null, error: 'provider_attempt_released_not_sent', recoverable: true,
      initiator_user_id: 1, source_message_id: 60,
    }] })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('平台已通过双人复核确认上次请求未发送')
    expect(wrapper.findAll('button').some(item => item.text() === '恢复运行')).toBe(true)
    wrapper.unmount()
  })

  it('账单已结算但答复检查点缺失时不允许自动恢复', async () => {
    mock.listEnterpriseAssistantRuns.mockResolvedValueOnce({ items: [{
      run_id: 'ear_settled_missing', status: 'awaiting_reconciliation', assistant_mode: 'enterprise_agent_runtime',
      message: { id: 65, conversation_id: 9, sender_id: null, sender_type: 'assistant',
        content: '小菱答复尚未保存。', client_message_id: 'ear_settled_missing' },
      pending: null, error: 'provider_result_missing_after_settlement', recoverable: false,
      initiator_user_id: 1, source_message_id: 60,
    }] })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('模型账单已核对，但答复检查点没有保存')
    expect(wrapper.findAll('button').some(item => item.text() === '恢复运行')).toBe(false)
    wrapper.unmount()
  })

  it('刷新后从当前账号会话恢复待处理的小菱任务，不重发原聊天消息', async () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'recovery-message', subtle: undefined })
    mock.sendEnterpriseMessage.mockResolvedValue({
      message: { id: 53, conversation_id: 9, sender_id: 1, sender_type: 'human',
        content: '@小菱 总结讨论', client_message_id: 'recovery-message' },
      assistant_available: true,
    })
    mock.requestEnterpriseAssistant.mockRejectedValueOnce(new Error('unknown'))
      .mockResolvedValueOnce({ run_id: 'ear_recovery', status: 'completed', assistant_mode: 'enterprise_agent_runtime',
        message: { id: 54, conversation_id: 9, sender_id: null, sender_type: 'assistant',
          content: '讨论已总结。', client_message_id: 'ear_recovery' } })

    const firstTab = mount(EnterpriseCommunication)
    await flushPromises()
    await firstTab.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    await firstTab.get('#message-content').setValue('@小菱 总结讨论')
    await firstTab.get('.composer').trigger('submit')
    await flushPromises()
    expect(sessionStorage.getItem('prism:assistant-recovery:1:10:9')).toBe('53')
    expect(sessionStorage.getItem('prism:assistant-recovery:2:10:9')).toBeNull()
    expect(sessionStorage.getItem('prism:assistant-recovery:1:11:9')).toBeNull()
    expect(sessionStorage.getItem('prism:assistant-recovery:1:10:99')).toBeNull()
    firstTab.unmount()

    const reloadedTab = mount(EnterpriseCommunication)
    await flushPromises()
    await reloadedTab.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    expect(reloadedTab.text()).toContain('发现一条未确认的小菱请求')
    await reloadedTab.findAll('button').find(item => item.text() === '检查后重试小菱')!.trigger('click')
    await flushPromises()
    expect(mock.sendEnterpriseMessage).toHaveBeenCalledTimes(1)
    expect(mock.requestEnterpriseAssistant).toHaveBeenCalledTimes(2)
    expect(mock.requestEnterpriseAssistant).toHaveBeenLastCalledWith(10, 9, 53, null)
    expect(reloadedTab.text()).toContain('讨论已总结。')
    expect(sessionStorage.getItem('prism:assistant-recovery:1:10:9')).toBeNull()
    reloadedTab.unmount()
  })

  it('群聊可以弹出为不遮挡页面操作的微信式悬浮窗口并收回', async () => {
    localStorage.removeItem('prism-floating-chat-position:enterprise')
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    const panel = wrapper.get('.chat-pane').element as HTMLElement
    Object.defineProperty(panel, 'offsetWidth', { configurable: true, value: 400 })
    Object.defineProperty(panel, 'offsetHeight', { configurable: true, value: 620 })
    await wrapper.find('.floating-toggle').trigger('click')
    await flushPromises()
    const dialog = document.body.querySelector('[role="dialog"]')
    expect(dialog).not.toBeNull()
    expect(dialog?.getAttribute('aria-modal')).toBeNull()
    expect(dialog?.textContent).toContain('公司群')
    expect(panel.style.width).toBe('400px')
    expect(panel.style.height).toBe('620px')
    const initialLeft = Number.parseFloat(panel.style.left)
    const pointer = (type: string, values: Record<string, number>) => {
      const event = new Event(type, { bubbles: true, cancelable: true }) as PointerEvent
      Object.entries(values).forEach(([key, value]) => Object.defineProperty(event, key, { value }))
      return event
    }
    dialog?.querySelector('.chat-drag-handle')?.dispatchEvent(pointer('pointerdown', {
      button: 0, clientX: 100, clientY: 100, pointerId: 1,
    }))
    await flushPromises()
    expect(panel.classList.contains('is-dragging')).toBe(true)
    panel.dispatchEvent(pointer('pointermove', { clientX: 60, clientY: 120, pointerId: 1 }))
    await flushPromises()
    panel.dispatchEvent(pointer('pointerup', { clientX: 60, clientY: 120, pointerId: 1 }))
    await flushPromises()
    expect(Number.parseFloat(panel.style.left)).toBe(initialLeft - 40)
    const dragHandle = dialog?.querySelector<HTMLButtonElement>('.chat-drag-handle')
    dragHandle?.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true, cancelable: true }))
    await flushPromises()
    expect(Number.parseFloat(panel.style.left)).toBe(initialLeft - 24)
    const initialWidth = Number.parseFloat(panel.style.width)
    dialog?.querySelector<HTMLElement>('.panel-resize-handle[data-direction="se"]')?.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }),
    )
    await flushPromises()
    expect(Number.parseFloat(panel.style.width)).toBe(initialWidth + 16)
    expect(document.body.querySelector('.chat-window-backdrop')).toBeNull()
    document.querySelector('.page-header h1')?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(document.body.querySelector('[role="dialog"]')).toBe(dialog)
    ;(dialog?.querySelector('.floating-toggle') as HTMLButtonElement).click()
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(wrapper.find('#message-content').exists()).toBe(true)
    wrapper.unmount()
    localStorage.removeItem('prism-floating-chat-position:enterprise')
  })

  it('桌面悬浮聊天在切换企业功能页时保持打开并可收回到聊天页', async () => {
    const wrapper = mount(EnterpriseCommunication, {
      attachTo: document.body,
      global: { stubs: { EnterpriseCommunity: { template: '<section data-test="enterprise-community">企业社区内容</section>' } } },
    })
    await flushPromises()
    await wrapper.findAll('.conversation-quick-actions button').find(item => item.text().includes('公司群'))!.trigger('click')
    await flushPromises()
    ;(document.body.querySelector('.floating-toggle') as HTMLButtonElement).click()
    await flushPromises()
    const dialog = document.body.querySelector('[role="dialog"]')
    expect(dialog?.textContent).toContain('公司群')

    await wrapper.findAll('.workspace-tabs button').find(item => item.text() === '企业论坛')!.trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="enterprise-community"]').exists()).toBe(true)
    expect(document.body.querySelector('[role="dialog"]')).toBe(dialog)
    expect(wrapper.find('.chat-shell.floating-only').exists()).toBe(true)

    ;(document.body.querySelector('.floating-toggle') as HTMLButtonElement).click()
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(wrapper.find('[data-test="enterprise-community"]').exists()).toBe(true)
    expect(document.activeElement).toBe(wrapper.get('.workspace-tabs button[aria-pressed="true"]').element)
    await wrapper.findAll('.workspace-tabs button').find(item => item.text() === '聊天')!.trigger('click')
    expect(wrapper.find('.chat-title-block').text()).toContain('公司群')
    wrapper.unmount()
  })

  it.each([
    { width: 760, height: 900, label: '窄视口' },
    { width: 1024, height: 520, label: '短视口' },
  ])('$label 全屏聊天进入审批页时先收起窗口并把焦点交给新页面', async ({ width, height }) => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: height })
    mock.listEnterpriseAssistantRuns.mockResolvedValueOnce({ items: [{
      run_id: 'ear_waiting_approval', status: 'waiting_approval', assistant_mode: 'enterprise_agent_runtime',
      message: null, pending: { kind: 'approval', call_id: 'call_approval_1', operation: 'create_announcement', approval_id: 5 },
      recoverable: false, initiator_user_id: 1, source_message_id: 50,
    }] })
    const wrapper = mount(EnterpriseCommunication, {
      attachTo: document.body,
      global: { stubs: { EnterpriseAssistantApprovals: { template: '<section data-test="assistant-approvals">审批内容</section>' } } },
    })
    await flushPromises()
    await wrapper.findAll('.conversation-quick-actions button').find(item => item.text().includes('公司群'))!.trigger('click')
    await flushPromises()
    ;(document.body.querySelector('.floating-toggle') as HTMLButtonElement).click()
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()

    ;(document.body.querySelector('.assistant-run-actions button') as HTMLButtonElement).click()
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(wrapper.find('[data-test="assistant-approvals"]').exists()).toBe(true)
    expect(document.activeElement).toBe(wrapper.get('.workspace-tabs button[aria-pressed="true"]').element)
    wrapper.unmount()
  })

  it('Escape 尊重页面控件与 IME，并关闭浮窗后把焦点还给打开按钮', async () => {
    const wrapper = mount(EnterpriseCommunication, { attachTo: document.body })
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    const opener = wrapper.get('.floating-toggle').element as HTMLButtonElement
    opener.click()
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()

    const companySelect = wrapper.get('#company-select').element as HTMLSelectElement
    companySelect.focus()
    companySelect.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()

    const heading = wrapper.get('h1').element as HTMLHeadingElement
    heading.tabIndex = -1
    heading.focus()
    heading.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true, isComposing: true }))
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()

    const cancelEscape = (event: KeyboardEvent) => event.preventDefault()
    heading.addEventListener('keydown', cancelEscape, { once: true })
    heading.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()

    heading.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(document.activeElement).toBe(opener)
    wrapper.unmount()
  })

  it('企业群聊可以打开并收起独立团队进度区', async () => {
    const wrapper = mount(EnterpriseCommunication, {
      global: { stubs: { EnterpriseAgentTeamProgress: { template: '<section data-test="team-progress-panel">团队进度面板</section>' } } },
    })
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()

    const toggle = () => wrapper.findAll('button').find(item => item.text() === '团队进度' || item.text() === '收起团队')!
    expect(toggle().attributes('aria-pressed')).toBe('false')
    await toggle().trigger('click')
    expect(wrapper.find('[data-test="team-progress-panel"]').exists()).toBe(true)
    expect(toggle().attributes('aria-pressed')).toBe('true')
    await toggle().trigger('click')
    expect(wrapper.find('[data-test="team-progress-panel"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('切换企业或失去小组会话权限时自动收回浮窗并清空旧内容', async () => {
    mock.listMyEnterprises.mockResolvedValue([
      { id: 10, name: '甲公司', role: 'owner', status: 'active' },
      { id: 20, name: '乙公司', role: 'member', status: 'active' },
    ])
    mock.listEnterpriseGroups.mockResolvedValue([{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'leader' }])
    mock.openEnterpriseConversation.mockImplementation(async (id, input) => ({
      id: 9, enterprise_id: id, kind: input.kind, group_id: input.group_id ?? null,
    }))
    mock.listEnterpriseAssistantRuns.mockResolvedValue({ items: [], assistant_mode: 'enterprise_agent_runtime' })
    sessionStorage.setItem('prism:assistant-recovery:1:10:9', '53')
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '研发组')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('发现一条未确认的小菱请求')
    await wrapper.get('#message-content').setValue('小组内部草稿')
    await wrapper.find('.floating-toggle').trigger('click')
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()

    mock.listEnterpriseGroups.mockResolvedValue([])
    await wrapper.findAll('button').find(item => item.text() === '刷新企业')!.trigger('click')
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(wrapper.text()).toContain('小组会话权限已变化')
    expect(wrapper.text()).not.toContain('小组内部草稿')
    expect(wrapper.text()).not.toContain('发现一条未确认的小菱请求')
    expect(sessionStorage.getItem('prism:assistant-recovery:1:10:9')).toBeNull()

    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    await wrapper.find('.floating-toggle').trigger('click')
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull()
    await wrapper.get('#company-select').setValue('20')
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    wrapper.unmount()
  })

  it('撤销小组权限后丢弃在途的旧会话打开响应', async () => {
    let resolveOpen!: (value: unknown) => void
    mock.listEnterpriseGroups.mockResolvedValue([{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'leader' }])
    mock.openEnterpriseConversation.mockReturnValue(new Promise(resolve => { resolveOpen = resolve }))
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()

    await wrapper.findAll('button').find(item => item.text() === '研发组')!.trigger('click')
    expect(mock.openEnterpriseConversation).toHaveBeenCalledTimes(1)
    mock.listEnterpriseGroups.mockResolvedValue([])
    await wrapper.findAll('button').find(item => item.text() === '刷新企业')!.trigger('click')
    await flushPromises()
    expect(wrapper.findAll('button').some(item => item.text() === '研发组')).toBe(false)

    resolveOpen({ id: 20, enterprise_id: 10, kind: 'group', group_id: 22 })
    await flushPromises()
    expect(wrapper.find('.chat-welcome').exists()).toBe(true)
    expect(wrapper.find('.floating-toggle').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('研发组会话')
    wrapper.unmount()
  })

  it('切换企业后丢弃旧企业迟到的结果', async () => {
    let resolveFirst!: (value: unknown) => void
    const delayed = new Promise(resolve => { resolveFirst = resolve })
    mock.listMyEnterprises.mockResolvedValue([
      { id: 10, name: '甲公司', role: 'member', status: 'active' },
      { id: 20, name: '乙公司', role: 'member', status: 'active' },
    ])
    mock.listEnterpriseGroups.mockImplementation((id: number) => id === 10 ? delayed : Promise.resolve([
      { id: 2, enterprise_id: 20, name: '乙组', status: 'active', my_role: 'member' },
    ]))
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.get('#company-select').setValue('20')
    await flushPromises()
    resolveFirst([{ id: 1, enterprise_id: 10, name: '甲组', status: 'active', my_role: 'leader' }])
    await flushPromises()
    expect(wrapper.text()).toContain('乙组')
    expect(wrapper.text()).not.toContain('甲组')
    wrapper.unmount()
  })

  it('已有企业成员仍能看到加入其他企业的入口，刷新不清空草稿', async () => {
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.get('[data-action="join-enterprise"]').trigger('click')
    expect(wrapper.find('#invite-token').exists()).toBe(true)
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    await wrapper.get('#message-content').setValue('未发送草稿')
    await wrapper.findAll('button').find(item => item.text() === '刷新企业')!.trigger('click')
    await flushPromises()
    expect((wrapper.get('#message-content').element as HTMLTextAreaElement).value).toBe('未发送草稿')
    wrapper.unmount()
  })

  it('切换会话和返回列表后，未发送草稿只回到原会话', async () => {
    mock.listEnterpriseGroups.mockResolvedValue([{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'member' }])
    mock.openEnterpriseConversation.mockImplementation(async (id, input) => ({
      id: input.kind === 'company' ? 9 : 20, enterprise_id: id, kind: input.kind, group_id: input.group_id ?? null,
    }))
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    await wrapper.get('#message-content').setValue('公司群未发送内容')
    await wrapper.findAll('button').find(item => item.text() === '研发组')!.trigger('click'); await flushPromises()
    expect((wrapper.get('#message-content').element as HTMLTextAreaElement).value).toBe('')
    await wrapper.get('#message-content').setValue('小组群未发送内容')
    await wrapper.get('[aria-label="返回会话列表"]').trigger('click'); await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    expect((wrapper.get('#message-content').element as HTMLTextAreaElement).value).toBe('公司群未发送内容')
    await wrapper.findAll('button').find(item => item.text() === '研发组')!.trigger('click'); await flushPromises()
    expect((wrapper.get('#message-content').element as HTMLTextAreaElement).value).toBe('小组群未发送内容')
    wrapper.unmount()
  })

  it('已发送成功的旧会话草稿不会因会话切换再次恢复', async () => {
    let resolveSend!: (value: unknown) => void
    mock.listEnterpriseGroups.mockResolvedValue([{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'member' }])
    mock.openEnterpriseConversation.mockImplementation(async (id, input) => ({
      id: input.kind === 'company' ? 9 : 20, enterprise_id: id, kind: input.kind, group_id: input.group_id ?? null,
    }))
    mock.sendEnterpriseMessage.mockReturnValue(new Promise(resolve => { resolveSend = resolve }))
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    await wrapper.get('#message-content').setValue('已发送内容')
    await wrapper.get('.composer').trigger('submit'); await flushPromises()
    // 发送前需等待异步摘要完成，确保切换时请求确实已发出。
    await vi.waitFor(() => expect(mock.sendEnterpriseMessage).toHaveBeenCalledTimes(1))
    await wrapper.findAll('button').find(item => item.text() === '研发组')!.trigger('click'); await flushPromises()
    resolveSend({ message: { id: 4, conversation_id: 9, sender_id: 1, content: '已发送内容' }, assistant_available: false })
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    expect((wrapper.get('#message-content').element as HTMLTextAreaElement).value).toBe('')
    wrapper.unmount()
  })

  it('重复选择当前会话会重新检查访问并保留输入', async () => {
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    await wrapper.get('#message-content').setValue('尚未发送')
    const count = mock.getEnterpriseMessages.mock.calls.length
    await wrapper.get('.room-row.selected').trigger('click'); await flushPromises()
    expect(mock.getEnterpriseMessages.mock.calls.length).toBe(count + 1)
    expect((wrapper.get('#message-content').element as HTMLTextAreaElement).value).toBe('尚未发送')
    wrapper.unmount()
  })

  it('切换账号后旧会话草稿不会恢复', async () => {
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    await wrapper.get('#message-content').setValue('旧账号的草稿')
    await wrapper.get('[aria-label="返回会话列表"]').trigger('click'); await flushPromises()
    useUserStore().profile!.id = 2; await flushPromises()
    useUserStore().profile!.id = 1; await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    expect((wrapper.get('#message-content').element as HTMLTextAreaElement).value).toBe('')
    wrapper.unmount()
  })

  it('小组撤权后清除已离开会话的草稿，重新加入也不会恢复', async () => {
    const group = { id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'member' }
    let member = true
    mock.listEnterpriseGroups.mockImplementation(async () => member ? [group] : [])
    mock.openEnterpriseConversation.mockImplementation(async (id, input) => ({
      id: input.kind === 'company' ? 9 : 20, enterprise_id: id, kind: input.kind, group_id: input.group_id ?? null,
    }))
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '研发组')!.trigger('click'); await flushPromises()
    await wrapper.get('#message-content').setValue('小组私有草稿')
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    member = false
    await wrapper.findAll('button').find(item => item.text() === '刷新企业')!.trigger('click'); await flushPromises()
    member = true
    await wrapper.findAll('button').find(item => item.text() === '刷新企业')!.trigger('click'); await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '研发组')!.trigger('click'); await flushPromises()
    expect((wrapper.get('#message-content').element as HTMLTextAreaElement).value).toBe('')
    wrapper.unmount()
  })

  it('访问检查未完成前不恢复草稿也不允许发送', async () => {
    let resolveMessages!: (value: unknown) => void
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    await wrapper.get('#message-content').setValue('必须保密的草稿')
    await wrapper.get('[aria-label="返回会话列表"]').trigger('click'); await flushPromises()
    mock.getEnterpriseMessages.mockReturnValueOnce(new Promise(resolve => { resolveMessages = resolve }))
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    expect((wrapper.get('#message-content').element as HTMLTextAreaElement).disabled).toBe(true)
    expect((wrapper.get('#message-content').element as HTMLTextAreaElement).value).toBe('')
    resolveMessages({ items: [], assistant_available: false }); await flushPromises()
    expect((wrapper.get('#message-content').element as HTMLTextAreaElement).disabled).toBe(false)
    expect((wrapper.get('#message-content').element as HTMLTextAreaElement).value).toBe('必须保密的草稿')
    wrapper.unmount()
  })

  it('重新打开会话遭拒时清除该企业草稿，恢复访问也不暴露旧内容', async () => {
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    await wrapper.get('#message-content').setValue('已撤权的草稿')
    await wrapper.get('[aria-label="返回会话列表"]').trigger('click'); await flushPromises()
    mock.getEnterpriseMessages.mockRejectedValueOnce({ code: 40300 })
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('权限')
    expect(wrapper.find('#message-content').exists()).toBe(false)
    await wrapper.findAll('button').find(item => item.text() === '刷新企业')!.trigger('click'); await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    expect((wrapper.get('#message-content').element as HTMLTextAreaElement).value).toBe('')
    wrapper.unmount()
  })

  it('连续打开不同会话时只接受最后一次选择', async () => {
    let resolveFirst!: (value: unknown) => void
    const delayed = new Promise(resolve => { resolveFirst = resolve })
    mock.listEnterpriseGroups.mockResolvedValue([{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'member' }])
    mock.openEnterpriseConversation.mockImplementation((id, input) => input.kind === 'company'
      ? delayed
      : Promise.resolve({ id: 20, enterprise_id: id, kind: 'group', group_id: 22 }))
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await wrapper.findAll('button').find(item => item.text() === '研发组')!.trigger('click')
    await flushPromises()
    resolveFirst({ id: 9, enterprise_id: 10, kind: 'company', group_id: null })
    await flushPromises()
    expect(wrapper.get('.chat h2').text()).toBe('研发组')
    wrapper.unmount()
  })

  it('加载企业失败时不误报没有加入企业', async () => {
    mock.listMyEnterprises.mockRejectedValue(new Error('offline'))
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    expect(wrapper.text()).toContain('企业列表加载失败')
    expect(wrapper.text()).not.toContain('还没有加入企业')
    wrapper.unmount()
  })

  it('会话权限撤销时清除消息并关闭会话', async () => {
    mock.getEnterpriseMessages.mockResolvedValueOnce({
      items: [{ id: 1, conversation_id: 9, sender_id: 1, content: '企业内部内容' }], assistant_available: false,
    }).mockRejectedValueOnce({ code: 40300 })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('企业内部内容')
    await wrapper.findAll('button').find(item => item.text() === '刷新消息')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('企业内部内容')
    expect(wrapper.text()).toContain('权限')
    wrapper.unmount()
  })

  it('发送结果未知后切换会话，重试同一内容沿用幂等键', async () => {
    let sequence = 0
    vi.stubGlobal('crypto', {
      randomUUID: () => `message-${++sequence}`,
      subtle: { digest: async () => new Uint8Array([1, 2, 3]).buffer },
    })
    mock.listEnterpriseGroups.mockResolvedValue([{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'member' }])
    mock.openEnterpriseConversation.mockImplementation(async (id, input) => ({
      id: input.kind === 'company' ? 9 : 20, enterprise_id: id, kind: input.kind, group_id: input.group_id ?? null,
    }))
    mock.sendEnterpriseMessage.mockRejectedValueOnce(new Error('unknown')).mockImplementationOnce(
      async (_company, conversation, content, key) => ({ message: { id: 4, conversation_id: conversation,
        sender_id: 1, content, client_message_id: key }, assistant_available: false }),
    )
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    await wrapper.get('#message-content').setValue('未确认消息')
    await wrapper.get('.composer').trigger('submit'); await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '研发组')!.trigger('click'); await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    await wrapper.get('#message-content').setValue('未确认消息')
    await wrapper.get('.composer').trigger('submit'); await flushPromises()
    expect(mock.sendEnterpriseMessage.mock.calls[0][3]).toBe(mock.sendEnterpriseMessage.mock.calls[1][3])
    wrapper.unmount()
  })

  it('一个会话发送中不阻塞另一个会话发消息', async () => {
    vi.stubGlobal('crypto', {
      randomUUID: () => 'message-token',
      subtle: { digest: async () => new Uint8Array([1, 2, 3]).buffer },
    })
    mock.listEnterpriseGroups.mockResolvedValue([{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'member' }])
    mock.openEnterpriseConversation.mockImplementation(async (id, input) => ({
      id: input.kind === 'company' ? 9 : 20, enterprise_id: id, kind: input.kind, group_id: input.group_id ?? null,
    }))
    mock.sendEnterpriseMessage.mockImplementationOnce(() => new Promise(() => {}))
      .mockImplementationOnce(async (_company, conversation, content, key) => ({
        message: { id: 7, conversation_id: conversation, sender_id: 1, content, client_message_id: key },
        assistant_available: false,
      }))
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    await wrapper.get('#message-content').setValue('甲')
    await wrapper.get('.composer').trigger('submit'); await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '研发组')!.trigger('click'); await flushPromises()
    await wrapper.get('#message-content').setValue('乙')
    await wrapper.get('.composer').trigger('submit'); await flushPromises()
    expect(mock.sendEnterpriseMessage).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('乙')
    wrapper.unmount()
  })

  it('账号切换后丢弃前一账号迟到的企业列表', async () => {
    let resolveFirst!: (value: unknown) => void
    const delayed = new Promise(resolve => { resolveFirst = resolve })
    mock.listMyEnterprises.mockReturnValueOnce(delayed).mockResolvedValueOnce([
      { id: 20, name: '新账号企业', role: 'member', status: 'active' },
    ])
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    useUserStore().profile!.id = 2
    await flushPromises()
    await vi.waitFor(() => expect(mock.listMyEnterprises).toHaveBeenCalledTimes(2))
    await vi.waitFor(() => expect(wrapper.text()).toContain('新账号企业'))
    resolveFirst([{ id: 10, name: '旧账号企业', role: 'member', status: 'active' }])
    await flushPromises()
    expect(wrapper.text()).toContain('新账号企业')
    expect(wrapper.text()).not.toContain('旧账号企业')
    wrapper.unmount()
  })

  it('账号切换时关闭浮窗并清除前一账号的群聊内容和草稿', async () => {
    mock.listMyEnterprises.mockResolvedValueOnce([{ id: 10, name: '旧账号企业', role: 'member', status: 'active' }])
      .mockResolvedValueOnce([{ id: 20, name: '新账号企业', role: 'member', status: 'active' }])
    mock.getEnterpriseMessages.mockResolvedValueOnce({ items: [
      { id: 70, conversation_id: 9, sender_id: 2, content: '旧账号群聊内容' },
    ], assistant_available: false })
    const wrapper = mount(EnterpriseCommunication, { attachTo: document.body })
    await flushPromises()
    await wrapper.findAll('.conversation-quick-actions button').find(item => item.text().includes('公司群'))!.trigger('click')
    await flushPromises()
    await wrapper.get('#message-content').setValue('旧账号草稿')
    ;(document.body.querySelector('.floating-toggle') as HTMLButtonElement).click()
    await flushPromises()
    expect(document.body.querySelector('[role="dialog"]')?.textContent).toContain('旧账号群聊内容')
    expect(document.activeElement).toBe(document.body.querySelector('[role="dialog"]'))

    useUserStore().profile!.id = 2
    await vi.waitFor(() => expect(wrapper.text()).toContain('新账号企业'))
    expect(document.body.querySelector('[role="dialog"]')).toBeNull()
    expect(document.body.textContent).not.toContain('旧账号群聊内容')
    expect(document.body.textContent).not.toContain('旧账号草稿')
    expect(document.activeElement).toBe(wrapper.get('.page-header h1').element)
    wrapper.unmount()
  })

  it('账号切换的同一更新周期内外部取得焦点时不抢回焦点', async () => {
    mock.listMyEnterprises.mockResolvedValueOnce([{ id: 10, name: '旧账号企业', role: 'member', status: 'active' }])
      .mockResolvedValueOnce([{ id: 20, name: '新账号企业', role: 'member', status: 'active' }])
    const wrapper = mount(EnterpriseCommunication, { attachTo: document.body })
    const externalControl = document.createElement('button')
    externalControl.textContent = '外部对话框'
    document.body.append(externalControl)
    try {
      await flushPromises()
      await wrapper.findAll('.conversation-quick-actions button').find(item => item.text().includes('公司群'))!.trigger('click')
      await flushPromises()
      ;(document.body.querySelector('.floating-toggle') as HTMLButtonElement).click()
      await flushPromises()
      expect(document.activeElement).toBe(document.body.querySelector('[role="dialog"]'))

      useUserStore().profile!.id = 2
      externalControl.focus()
      await vi.waitFor(() => expect(wrapper.text()).toContain('新账号企业'))
      expect(document.activeElement).toBe(externalControl)
    } finally {
      externalControl.remove()
      wrapper.unmount()
    }
  })

  it('账号切换后迟到的消息发送响应不能覆盖新账号的小菱可用状态', async () => {
    let resolveOldSend!: (value: unknown) => void
    mock.listMyEnterprises.mockResolvedValueOnce([{ id: 10, name: '旧账号企业', role: 'member', status: 'active' }])
      .mockResolvedValueOnce([{ id: 20, name: '新账号企业', role: 'member', status: 'active' }])
    mock.sendEnterpriseMessage.mockReturnValueOnce(new Promise(resolve => { resolveOldSend = resolve }))
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    await wrapper.get('#message-content').setValue('旧账号消息')
    await wrapper.get('.composer').trigger('submit')
    await vi.waitFor(() => expect(mock.sendEnterpriseMessage).toHaveBeenCalledTimes(1))

    useUserStore().profile!.id = 2
    await vi.waitFor(() => expect(wrapper.text()).toContain('新账号企业'))
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('小菱暂不可用')

    resolveOldSend({
      message: { id: 70, conversation_id: 9, sender_id: 1, content: '旧账号消息' },
      assistant_available: true,
    })
    await flushPromises()
    expect(wrapper.text()).toContain('小菱暂不可用')
    expect(wrapper.text()).not.toContain('旧账号消息')
    wrapper.unmount()
  })

  it('账号切回时不接受旧会话结果，也不保留旧邀请码', async () => {
    let resolveOld!: (value: unknown) => void
    mock.listMyEnterprises.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve }))
      .mockResolvedValueOnce([{ id: 20, name: '账号乙', role: 'member' }])
      .mockResolvedValueOnce([{ id: 30, name: '账号甲的新会话', role: 'member' }])
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.get('[data-action="join-enterprise"]').trigger('click')
    await wrapper.get('#invite-token').setValue('私人邀请码')
    useUserStore().profile!.id = 2; await flushPromises()
    await vi.waitFor(() => expect(wrapper.text()).toContain('账号乙'))
    useUserStore().profile!.id = 1; await flushPromises()
    await vi.waitFor(() => expect(wrapper.text()).toContain('账号甲的新会话'))
    resolveOld([{ id: 10, name: '过期会话', role: 'member' }]); await flushPromises()
    expect(wrapper.text()).toContain('账号甲的新会话')
    expect(wrapper.text()).not.toContain('过期会话')
    await wrapper.get('[data-action="join-enterprise"]').trigger('click')
    expect((wrapper.get('#invite-token').element as HTMLInputElement).value).toBe('')
    wrapper.unmount()
  })

  it('发送时撤权后清除内容，刷新企业能恢复可访问的会话', async () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'message-token',
      subtle: { digest: async () => new Uint8Array([1, 2, 3]).buffer } })
    mock.getEnterpriseMessages.mockResolvedValue({ items: [
      { id: 1, conversation_id: 9, sender_id: 1, content: '旧会话秘密' },
    ] })
    mock.sendEnterpriseMessage.mockRejectedValue({ code: 40300 })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    await wrapper.get('#message-content').setValue('消息')
    await wrapper.get('.composer').trigger('submit'); await flushPromises()
    expect(wrapper.text()).not.toContain('旧会话秘密')
    expect(wrapper.text()).toContain('权限')
    const calls = mock.listEnterpriseGroups.mock.calls.length
    await wrapper.findAll('button').find(item => item.text() === '刷新企业')!.trigger('click'); await flushPromises()
    expect(mock.listEnterpriseGroups.mock.calls.length).toBeGreaterThan(calls)
    wrapper.unmount()
  })

  it('生成消息请求键期间凭据切换时不发送旧草稿', async () => {
    let resolveDigest!: (value: ArrayBuffer) => void
    vi.stubGlobal('crypto', { randomUUID: () => 'message-token',
      subtle: { digest: () => new Promise(resolve => { resolveDigest = resolve }) } })
    localStorage.setItem('review_token', 'old-token')
    const wrapper = mount(EnterpriseCommunication); await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '公司群')!.trigger('click'); await flushPromises()
    await wrapper.get('#message-content').setValue('旧账号草稿')
    await wrapper.get('.composer').trigger('submit')
    localStorage.setItem('review_token', 'new-token')
    resolveDigest(new Uint8Array([1, 2, 3]).buffer); await flushPromises()
    expect(mock.sendEnterpriseMessage).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('论坛和公告只显示当前企业的真实数据', async () => {
    mock.listEnterprisePosts.mockResolvedValue({ items: [
      { id: 1, enterprise_id: 10, author_id: 1, title: '讨论测试', content: '实际帖子' },
    ] })
    mock.listEnterpriseAnnouncements.mockResolvedValue({ items: [
      { id: 2, enterprise_id: 10, author_id: 1, title: '通知测试', content: '实际公告' },
    ] })
    const wrapper = mount(EnterpriseCommunication)
    await flushPromises()
    await wrapper.findAll('button').find(item => item.text() === '企业论坛')!.trigger('click'); await flushPromises()
    expect(mock.listEnterprisePosts).toHaveBeenCalledWith(10)
    expect(wrapper.text()).toContain('实际帖子')
    await wrapper.findAll('button').find(item => item.text() === '企业公告')!.trigger('click'); await flushPromises()
    expect(mock.listEnterpriseAnnouncements).toHaveBeenCalledWith(10)
    expect(wrapper.text()).toContain('实际公告')
    expect(wrapper.text()).not.toContain('实际帖子')
    wrapper.unmount()
  })
})
