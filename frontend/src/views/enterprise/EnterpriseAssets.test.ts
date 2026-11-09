import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ list: vi.fn(), get: vi.fn(), create: vi.fn(), revise: vi.fn(), remove: vi.fn(),
  request: vi.fn(), mine: vi.fn(), pending: vi.fn(), withdraw: vi.fn(), decide: vi.fn(), revoke: vi.fn(),
  catalog: vi.fn() }))
const auth = vi.hoisted(() => ({ token: 'session-1' }))
const routeGuard = vi.hoisted(() => ({ leave: null as null | (() => boolean) }))
vi.mock('vue-router', () => ({ onBeforeRouteLeave: (guard: () => boolean) => { routeGuard.leave = guard } }))
vi.mock('@/api/enterpriseAssets', () => ({ listEnterpriseAssetDrafts: api.list, getEnterpriseAssetDraft: api.get,
  createEnterpriseAssetDraft: api.create, reviseEnterpriseAssetDraft: api.revise, deleteEnterpriseAssetDraft: api.remove,
  requestEnterpriseAssetPublication: api.request, listMyEnterpriseAssetPublications: api.mine,
  listPendingEnterpriseAssetPublications: api.pending, withdrawEnterpriseAssetPublication: api.withdraw,
  decideEnterpriseAssetPublication: api.decide, revokeEnterpriseAssetPublication: api.revoke,
  listEnterpriseAssetCatalog: api.catalog }))
vi.mock('@/utils/token', () => ({ getToken: () => auth.token }))
import EnterpriseAssets from './EnterpriseAssets.vue'

const draft = { id: 4, enterprise_id: 10, creator_id: 2, kind: 'agent', title: '我的草稿',
  definition: { prompt: '原内容' }, current_version: 1, status: 'active', published_scopes: [],
  sharing_available: false, execution_available: false }
const props = { enterpriseId: 10, currentUserId: 2 }
beforeEach(() => {
  auth.token = 'session-1'
  routeGuard.leave = null
  sessionStorage.clear()
  Object.values(api).forEach(fn => fn.mockReset())
  api.list.mockResolvedValue([draft]); api.get.mockResolvedValue(draft)
  api.mine.mockResolvedValue([]); api.pending.mockResolvedValue([]); api.catalog.mockResolvedValue([])
  vi.stubGlobal('crypto', { randomUUID: () => '11111111-1111-4111-8111-111111111111',
    subtle: { digest: async () => new Uint8Array([1, 2, 3]).buffer } })
})

describe('企业个人草稿', () => {
  it('未保存内容在切换草稿、新建、重新加载及离开页面时受保护', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const wrapper = mount(EnterpriseAssets, { props })
    await flushPromises(); await wrapper.get('[data-draft="4"]').trigger('click'); await flushPromises()
    await wrapper.get('#asset-title').setValue('未保存修改')
    await wrapper.get('[data-action="new-asset"]').trigger('click'); await flushPromises()
    expect((wrapper.get('#asset-title').element as HTMLInputElement).value).toBe('未保存修改')
    await wrapper.get('[data-draft="4"]').trigger('click'); await flushPromises()
    expect((wrapper.get('#asset-title').element as HTMLInputElement).value).toBe('未保存修改')
    expect(routeGuard.leave?.()).toBe(false)
    const unload = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(unload)
    expect(unload.defaultPrevented).toBe(true)
    expect(confirm).toHaveBeenCalled()
    wrapper.unmount(); confirm.mockRestore()
  })

  it('未保存编辑时不提交旧版本发布申请', async () => {
    const wrapper = mount(EnterpriseAssets, { props })
    await flushPromises(); await wrapper.get('[data-draft="4"]').trigger('click'); await flushPromises()
    await wrapper.get('#asset-title').setValue('未保存标题')
    await wrapper.findAll('button').find(button => button.text() === '申请企业内发布')!.trigger('click')
    await flushPromises()
    expect(api.request).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('请先保存当前修改')
    expect((wrapper.get('#asset-title').element as HTMLInputElement).value).toBe('未保存标题')
    wrapper.unmount()
  })

  it('打开草稿时用户继续编辑，迟到详情不能覆盖输入', async () => {
    let resolveDetail!: (value: unknown) => void
    api.get.mockResolvedValueOnce(draft).mockReturnValueOnce(new Promise(resolve => { resolveDetail = resolve }))
    const wrapper = mount(EnterpriseAssets, { props })
    await flushPromises(); await wrapper.get('[data-draft="4"]').trigger('click'); await flushPromises()
    await wrapper.get('[data-draft="4"]').trigger('click')
    await wrapper.get('#asset-title').setValue('加载中继续编辑')
    resolveDetail(draft); await flushPromises()
    expect((wrapper.get('#asset-title').element as HTMLInputElement).value).toBe('加载中继续编辑')
    expect(wrapper.text()).toContain('加载期间内容已修改')
    wrapper.unmount()
  })

  it('会话失效立即清除未保存内容并取消离开提示', async () => {
    const wrapper = mount(EnterpriseAssets, { props })
    await flushPromises(); await wrapper.get('[data-draft="4"]').trigger('click'); await flushPromises()
    await wrapper.get('#asset-title').setValue('私有修改')
    window.dispatchEvent(new Event('prism:auth-expired')); await flushPromises()
    expect(wrapper.find('#asset-title').exists()).toBe(false)
    expect(routeGuard.leave?.()).toBe(true)
    wrapper.unmount()
  })

  it('账号或企业切换立即清除未保存内容，不弹出阻挡隐私清理的确认', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const wrapper = mount(EnterpriseAssets, { props })
    await flushPromises(); await wrapper.get('[data-draft="4"]').trigger('click'); await flushPromises()
    await wrapper.get('#asset-title').setValue('旧用户私有内容')
    api.list.mockResolvedValue([])
    await wrapper.setProps({ enterpriseId: 20, currentUserId: 3 }); await flushPromises()
    expect(wrapper.text()).not.toContain('旧用户私有内容')
    expect(wrapper.find('#asset-title').exists()).toBe(false)
    expect(confirm).not.toHaveBeenCalled()
    wrapper.unmount(); confirm.mockRestore()
  })
  it('成员可打开草稿，按当前版本保存 JSON 并说明能力边界', async () => {
    api.revise.mockResolvedValue({ draft_id: 4, version_number: 2, title: '修改后', definition: { prompt: '新内容' } })
    const wrapper = mount(EnterpriseAssets, { props })
    await flushPromises()
    expect(wrapper.text()).toContain('目录逐项标出运行配置是否兼容')
    await wrapper.get('[data-draft="4"]').trigger('click'); await flushPromises()
    await wrapper.get('#asset-title').setValue('修改后')
    await wrapper.get('#asset-definition').setValue('{"prompt":"新内容"}')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(api.revise).toHaveBeenCalledWith(10, 4, expect.objectContaining({
      expected_version: 1, definition: { prompt: '新内容' }, request_key: expect.any(String),
    }), 'session-1')
    expect(wrapper.text()).toContain('版本 2')
    wrapper.unmount()
  })

  it('创建结果未知时重试保留相同幂等键', async () => {
    api.create.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(draft)
    const wrapper = mount(EnterpriseAssets, { props })
    await flushPromises()
    await wrapper.get('[data-action="new-asset"]').trigger('click')
    await wrapper.get('#asset-title').setValue('新草稿')
    await wrapper.get('#asset-definition').setValue('{"prompt":"new"}')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(wrapper.text()).toContain('结果未知')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(api.create.mock.calls[0][1].request_key).toBe(api.create.mock.calls[1][1].request_key)
    expect(api.create.mock.calls[0][2]).toBe('session-1')
    wrapper.unmount()
  })

  it('版本冲突保留用户编辑内容并要求刷新核对', async () => {
    api.revise.mockRejectedValue({ code: 40901 })
    const wrapper = mount(EnterpriseAssets, { props })
    await flushPromises(); await wrapper.get('[data-draft="4"]').trigger('click'); await flushPromises()
    await wrapper.get('#asset-definition').setValue('{"prompt":"我的修改"}')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(wrapper.text()).toContain('版本已变化')
    expect((wrapper.get('#asset-definition').element as HTMLTextAreaElement).value).toContain('我的修改')
    wrapper.unmount()
  })

  it('切换账号或企业后丢弃迟到内容', async () => {
    let resolveOld!: (value: unknown) => void
    api.list.mockImplementation((enterpriseId: number) => enterpriseId === 10
      ? new Promise(resolve => { resolveOld = resolve })
      : Promise.resolve([]))
    const wrapper = mount(EnterpriseAssets, { props })
    await flushPromises()
    await wrapper.setProps({ enterpriseId: 20, currentUserId: 3 }); await flushPromises()
    resolveOld([draft]); await flushPromises()
    expect(wrapper.find('[data-draft="4"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('原内容')
    expect(api.list).toHaveBeenLastCalledWith(20, 0, 'session-1')
    wrapper.unmount()
  })

  it('摘要计算期间换登录凭据后不会用新会话提交旧账号草稿', async () => {
    let resolveDigest!: (value: ArrayBuffer) => void
    vi.stubGlobal('crypto', { randomUUID: () => '11111111-1111-4111-8111-111111111111',
      subtle: { digest: () => new Promise<ArrayBuffer>(resolve => { resolveDigest = resolve }) } })
    const wrapper = mount(EnterpriseAssets, { props })
    await flushPromises()
    await wrapper.get('[data-action="new-asset"]').trigger('click')
    await wrapper.get('#asset-title').setValue('旧会话草稿')
    const submit = wrapper.get('form').trigger('submit')
    await Promise.resolve()
    auth.token = 'session-2'
    resolveDigest(new Uint8Array([4, 5, 6]).buffer)
    await submit; await flushPromises()
    expect(api.create).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('草稿已保存')
    wrapper.unmount()
  })

  it('撤销企业成员权限后清空内容并通知父页面', async () => {
    api.get.mockRejectedValue({ code: 40300 })
    const wrapper = mount(EnterpriseAssets, { props })
    await flushPromises(); await wrapper.get('[data-draft="4"]').trigger('click'); await flushPromises()
    expect(wrapper.find('[data-draft="4"]').exists()).toBe(false)
    expect(wrapper.find('#asset-title').exists()).toBe(false)
    expect(wrapper.emitted('access-lost')).toHaveLength(1)
    wrapper.unmount()
  })

  it('企业成员可以申请固定版本发布，并了解目录与小菱调用边界', async () => {
    api.request.mockResolvedValue({ id: 21, status: 'pending' })
    api.mine.mockResolvedValue([{ id: 21, draft_id: 4, version_id: 9, version_number: 1, requested_by: 2,
      kind: 'agent', scope: 'enterprise', status: 'pending', title: '我的草稿', definition: { prompt: '原内容' },
      content_sha256: 'a'.repeat(64), decided_by: null, decided_at: null, decision_reason: '',
      revoked_by: null, revoked_at: null, revocation_reason: '' }])
    const wrapper = mount(EnterpriseAssets, { props })
    await flushPromises(); await wrapper.get('[data-draft="4"]').trigger('click'); await flushPromises()
    const publishButton = wrapper.findAll('button').find(button => button.text() === '申请企业内发布')
    expect(publishButton).toBeTruthy()
    await publishButton!.trigger('click'); await flushPromises()
    expect(api.request).toHaveBeenCalledWith(10, 4, expect.objectContaining({ scope: 'enterprise', expected_version: 1 }), 'session-1')
    expect(wrapper.text()).toContain('发布申请已提交')
    expect(wrapper.text()).toContain('目录逐项标出运行配置是否兼容')
    wrapper.unmount()
  })

  it('企业管理员看到待审批项并能批准，同时企业普通成员看不到审批操作', async () => {
    const pending = { id: 22, draft_id: 4, version_id: 9, version_number: 1, requested_by: 3,
      kind: 'skill', scope: 'enterprise', status: 'pending', title: '共享技能', definition: { instructions: 'safe' },
      content_sha256: 'b'.repeat(64), decided_by: null, decided_at: null, decision_reason: '',
      revoked_by: null, revoked_at: null, revocation_reason: '' }
    api.pending.mockResolvedValue([pending])
    const admin = mount(EnterpriseAssets, { props: { ...props, role: 'admin' } })
    await flushPromises()
    await admin.findAll('button').find(button => button.text() === '发布申请')!.trigger('click'); await flushPromises()
    expect(admin.text()).toContain('待企业审批')
    expect(admin.text()).toContain('共享技能')
    api.decide.mockResolvedValue({ ...pending, status: 'approved' })
    await admin.findAll('button').find(button => button.text() === '批准企业内发布')!.trigger('click'); await flushPromises()
    expect(api.decide).toHaveBeenCalledWith(10, 22, { decision: 'approve', reason: '' }, 'session-1')
    admin.unmount()

    const member = mount(EnterpriseAssets, { props })
    await flushPromises(); await member.findAll('button').find(button => button.text() === '发布申请')!.trigger('click'); await flushPromises()
    expect(member.text()).not.toContain('待企业审批')
    expect(member.text()).not.toContain('批准企业内发布')
    member.unmount()
  })

  it('企业目录逐项说明固定版本是否兼容企业小菱运行时', async () => {
    api.catalog.mockResolvedValue([
      { id: 31, version_number: 2,
        kind: 'agent', scope: 'enterprise', status: 'approved', title: '可运行 Agent',
        definition: { schema_version: 1, runtime: 'prompt', system_prompt: '总结' },
        content_sha256: 'c'.repeat(64), execution_available: false,
        xiaoling_runtime_config_compatible: true, can_revoke: true },
      { id: 32, version_number: 1,
        kind: 'skill', scope: 'enterprise', status: 'approved', title: '旧版 Skill',
        definition: { prompt: '旧配置' }, content_sha256: 'd'.repeat(64), execution_available: false,
        xiaoling_runtime_config_compatible: false, can_revoke: false },
    ])
    const wrapper = mount(EnterpriseAssets, { props })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '企业目录')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('运行配置格式与大小兼容；实际调用仍受企业权限、计量、订阅、额度和模型配置限制')
    expect(wrapper.text()).toContain('当前小菱运行器不兼容此版本配置')
    expect(wrapper.findAll('button').map(button => button.text())).toContain('撤销企业发布')
    wrapper.unmount()
  })
})
