import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const mock = vi.hoisted(() => ({ list: vi.fn() }))
const auth = vi.hoisted(() => ({ token: 'global-session' }))
vi.mock('@/api/enterpriseAssets', () => ({ listGlobalEnterpriseAssetCatalog: mock.list }))
vi.mock('@/utils/token', () => ({ getToken: () => auth.token }))
vi.mock('./PublicPersonalAgents.vue', () => ({ default: { template: '<div />' } }))
const runner = vi.hoisted(() => ({ choose: vi.fn() }))
vi.mock('./GlobalEnterpriseAssetRunner.vue', () => ({ default: { methods: { choose: runner.choose }, template: '<div />' } }))

import GlobalEnterpriseAssetCatalog from './GlobalEnterpriseAssetCatalog.vue'
import { useUserStore } from '@/stores/user'

const item = { id: 18, version_number: 2, kind: 'skill',
  scope: 'global', status: 'approved', title: '共享格式化 Skill', definition: { rules: ['safe'] },
  execution_available: false, xiaoling_runtime_config_compatible: true, can_revoke: false,
  personal_execution_available: true,
  content_sha256: 'c'.repeat(64) }

beforeEach(() => {
  setActivePinia(createPinia())
  const user = useUserStore(); user.token = 'global-session'
  user.profile = { id: 10, username: 'member', role: 'user', status: 1 }
  auth.token = 'global-session'; mock.list.mockReset(); mock.list.mockResolvedValue([item])
})

describe('全局共享 Agent 与 Skill 目录', () => {
  it('展示固定版本并允许使用兼容的共享资产', async () => {
    const wrapper = mount(GlobalEnterpriseAssetCatalog)
    await flushPromises()
    expect(mock.list).toHaveBeenCalledWith('global-session')
    expect(wrapper.text()).toContain('共享格式化 Skill')
    expect(wrapper.text()).toContain('v2')
    expect(wrapper.text()).not.toContain('目录不能直接运行')
    await wrapper.get('[data-run-asset="18"]').trigger('click')
    expect(runner.choose).toHaveBeenCalledWith(item)
    wrapper.unmount()
  })

  it('凭据切换后丢弃旧会话迟到的目录结果', async () => {
    let resolveOld!: (value: unknown) => void
    mock.list.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
    const wrapper = mount(GlobalEnterpriseAssetCatalog)
    await flushPromises()
    auth.token = 'next-session'
    resolveOld([item]); await flushPromises()
    expect(wrapper.text()).not.toContain('共享格式化 Skill')
    wrapper.unmount()
  })

  it('账号切换时清除原目录，并等待对应的账号信息后重新加载', async () => {
    const wrapper = mount(GlobalEnterpriseAssetCatalog); await flushPromises()
    expect(wrapper.text()).toContain('共享格式化 Skill')
    const user = useUserStore()
    user.token = 'next-session'
    await nextTick()
    expect(wrapper.text()).not.toContain('共享格式化 Skill')
    expect(mock.list).toHaveBeenCalledTimes(1)
    auth.token = 'next-session'
    user.profile = { id: 11, username: 'enterprise-member', role: 'user', status: 1 }
    await flushPromises()
    expect(mock.list).toHaveBeenLastCalledWith('next-session')
    wrapper.unmount()
  })

  it('明确标出目录可见但运行时不支持的旧版本', async () => {
    mock.list.mockResolvedValue([{ ...item, xiaoling_runtime_config_compatible: false }])
    const wrapper = mount(GlobalEnterpriseAssetCatalog)
    await flushPromises()
    expect(wrapper.text()).toContain('暂不支持此版本')
    expect(wrapper.find('[data-run-asset="18"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('可继续浏览第 101 条共享资产，翻页使用末条 ID 而不重复第一页', async () => {
    const page = Array.from({ length: 100 }, (_, i) => ({ ...item, id: 200 - i, title: `资产 ${200 - i}` }))
    mock.list.mockResolvedValueOnce(page).mockResolvedValueOnce([{ ...item, id: 100, title: '较早资产' }])
    const wrapper = mount(GlobalEnterpriseAssetCatalog); await flushPromises()
    expect(wrapper.text()).not.toContain('较早资产')
    await wrapper.get('[data-action="more-assets"]').trigger('click'); await flushPromises()
    expect(mock.list).toHaveBeenLastCalledWith('global-session', 101)
    expect(wrapper.text()).toContain('较早资产')
    expect(wrapper.findAll('.asset-card')).toHaveLength(101)
    expect(wrapper.find('[data-action="more-assets"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('翻页中切换账号时丢弃旧响应并禁止重复请求', async () => {
    const page = Array.from({ length: 100 }, (_, i) => ({ ...item, id: 200 - i }))
    let finishPage!: (value: unknown) => void
    mock.list.mockResolvedValueOnce(page)
      .mockImplementationOnce(() => new Promise(resolve => { finishPage = resolve }))
      .mockResolvedValueOnce([{ ...item, id: 300, title: '新账号资产' }])
    const wrapper = mount(GlobalEnterpriseAssetCatalog); await flushPromises()
    await wrapper.get('[data-action="more-assets"]').trigger('click'); await nextTick()
    expect(wrapper.get('[data-action="more-assets"]').attributes('disabled')).toBeDefined()
    const user = useUserStore(); user.profile = null; auth.token = 'next-session'; user.token = 'next-session'
    user.profile = { id: 11, username: 'other', role: 'user', status: 1 }
    await flushPromises()
    finishPage([{ ...item, id: 100, title: '旧账号迟到资产' }]); await flushPromises()
    expect(wrapper.text()).toContain('新账号资产')
    expect(wrapper.text()).not.toContain('旧账号迟到资产')
    expect(mock.list).toHaveBeenCalledTimes(3)
    wrapper.unmount()
  })

  it('加载更多失败保留当前页面，刷新后移除已下架资产', async () => {
    const page = Array.from({ length: 100 }, (_, i) => ({ ...item, id: 200 - i }))
    mock.list.mockResolvedValueOnce(page).mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce([])
    const wrapper = mount(GlobalEnterpriseAssetCatalog); await flushPromises()
    await wrapper.get('[data-action="more-assets"]').trigger('click'); await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('加载失败')
    expect(wrapper.findAll('.asset-card')).toHaveLength(100)
    await wrapper.get('.page-heading button').trigger('click'); await flushPromises()
    expect(wrapper.findAll('.asset-card')).toHaveLength(0)
    expect(wrapper.text()).toContain('当前没有已批准的全局共享资产')
    wrapper.unmount()
  })

  it('首屏加载失败时不把未知目录误报为空，刷新成功后才显示空态', async () => {
    mock.list.mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce([])
    const wrapper = mount(GlobalEnterpriseAssetCatalog); await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('加载失败')
    expect(wrapper.text()).not.toContain('当前没有已批准的全局共享资产')
    await wrapper.get('.page-heading button').trigger('click'); await flushPromises()
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('当前没有已批准的全局共享资产')
    wrapper.unmount()
  })

  it('账号尚未就绪时不提前显示空目录', async () => {
    const user = useUserStore(); user.profile = null; user.token = ''; auth.token = ''
    const wrapper = mount(GlobalEnterpriseAssetCatalog); await flushPromises()
    expect(mock.list).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('当前没有已批准的全局共享资产')
    wrapper.unmount()
  })
})
