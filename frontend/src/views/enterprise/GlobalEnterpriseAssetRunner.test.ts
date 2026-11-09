import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

const api = vi.hoisted(() => ({ run: vi.fn(), byId: vi.fn(), byRequest: vi.fn() }))
const auth = vi.hoisted(() => ({ token: 'token-1' }))
vi.mock('@/api/enterpriseAssetRuns', () => ({ runEnterpriseAsset: api.run,
  getEnterpriseAssetRun: api.byId, getEnterpriseAssetRunByRequest: api.byRequest }))
vi.mock('@/utils/token', () => ({ getToken: () => auth.token, setToken: vi.fn(), clearToken: vi.fn() }))
vi.mock('@/components/agent/AgentRunHistory.vue', () => ({ default: { props: ['source'], template: '<div :data-history="source" />' } }))
import { useUserStore } from '@/stores/user'
import GlobalEnterpriseAssetRunner from './GlobalEnterpriseAssetRunner.vue'

const asset = { id: 3, title: '共享工具', version_number: 2, kind: 'skill' as const,
  personal_execution_available: true }
const result = { id: 7, enterprise_publication_id: 3, version_number: 2,
  status: 'succeeded', output: '处理完成', result_reason: null, actual_milli: 1200, created_at: '2026-11-09T00:00:00' }
beforeEach(() => {
  sessionStorage.clear(); setActivePinia(createPinia()); auth.token = 'token-1'
  const user = useUserStore(); user.token = auth.token
  user.profile = { id: 5, username: 'member', role: 'user', status: 1 }
  Object.values(api).forEach(fn => fn.mockReset())
  vi.stubGlobal('crypto', { randomUUID: () => 'enterprise-key-1',
    subtle: { digest: async (_: string, bytes: Uint8Array) => new Uint8Array([bytes.reduce((a, b) => (a + b) % 256, 0)]).buffer } })
})

describe('企业全局资产个人运行', () => {
  it('使用个人积分运行 Skill，结果与费用清晰显示', async () => {
    api.run.mockResolvedValue(result)
    const wrapper = mount(GlobalEnterpriseAssetRunner, { props: { items: [asset] } })
    await (wrapper.vm as unknown as { choose: (item: typeof asset) => void }).choose(asset); await nextTick()
    await wrapper.get('#enterprise-asset-input').setValue('原内容')
    await wrapper.get('[data-action="run-enterprise-public"]').trigger('click'); await flushPromises()
    expect(api.run).toHaveBeenCalledWith(3, { request_key: 'enterprise-key-1', input_text: '原内容' }, 'token-1')
    expect(wrapper.text()).toContain('处理完成'); expect(wrapper.text()).toContain('1.2 积分')
    expect(wrapper.get('[data-history]').attributes('data-history')).toBe('enterprise_public')
    wrapper.unmount()
  })
  it('响应未知时只允许同内容使用同一请求键重试', async () => {
    api.run.mockRejectedValueOnce(new Error('timeout')).mockResolvedValueOnce(result)
    const wrapper = mount(GlobalEnterpriseAssetRunner, { props: { items: [asset] } })
    ;(wrapper.vm as unknown as { choose: (item: typeof asset) => void }).choose(asset); await nextTick()
    await wrapper.get('#enterprise-asset-input').setValue('原内容')
    await wrapper.get('[data-action="run-enterprise-public"]').trigger('click'); await flushPromises()
    await wrapper.get('[data-action="run-enterprise-public"]').trigger('click'); await flushPromises()
    expect(api.run.mock.calls.map(call => call[1].request_key)).toEqual(['enterprise-key-1', 'enterprise-key-1'])
    expect(wrapper.text()).toContain('处理完成'); wrapper.unmount()
  })
  it('不允许更改未知请求的输入后重复发起', async () => {
    api.run.mockRejectedValueOnce(new Error('timeout'))
    const wrapper = mount(GlobalEnterpriseAssetRunner, { props: { items: [asset] } })
    ;(wrapper.vm as unknown as { choose: (item: typeof asset) => void }).choose(asset); await nextTick()
    await wrapper.get('#enterprise-asset-input').setValue('原内容')
    await wrapper.get('[data-action="run-enterprise-public"]').trigger('click'); await flushPromises()
    await wrapper.get('#enterprise-asset-input').setValue('改过的内容')
    await wrapper.get('[data-action="run-enterprise-public"]').trigger('click'); await flushPromises()
    expect(api.run).toHaveBeenCalledTimes(1); expect(wrapper.text()).toContain('内容与原请求不同')
    wrapper.unmount()
  })
  it('版本停用时不展示调用入口', async () => {
    const unavailable = { ...asset, personal_execution_available: false }
    const wrapper = mount(GlobalEnterpriseAssetRunner, { props: { items: [unavailable] } })
    ;(wrapper.vm as unknown as { choose: (item: typeof asset) => void }).choose(unavailable); await nextTick()
    expect(wrapper.find('[data-action="run-enterprise-public"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('暂不可运行'); wrapper.unmount()
  })
  it('下架后仍可查询本人已有运行且不允许新建运行', async () => {
    api.run.mockResolvedValue(result); api.byId.mockResolvedValue(result)
    let wrapper = mount(GlobalEnterpriseAssetRunner, { props: { items: [asset] } })
    ;(wrapper.vm as unknown as { choose: (item: typeof asset) => void }).choose(asset); await nextTick()
    await wrapper.get('#enterprise-asset-input').setValue('内容')
    await wrapper.get('[data-action="run-enterprise-public"]').trigger('click'); await flushPromises(); wrapper.unmount()
    wrapper = mount(GlobalEnterpriseAssetRunner, { props: { items: [] } }); await flushPromises()
    await wrapper.get('[data-saved-publication="3"]').trigger('click'); await flushPromises()
    expect(api.byId).toHaveBeenCalledWith(3, 7, 'token-1'); expect(wrapper.text()).toContain('处理完成')
    expect(wrapper.find('[data-action="run-enterprise-public"]').exists()).toBe(false); wrapper.unmount()
  })
  it('账号切换清理旧输入并丢弃迟到响应', async () => {
    let resolve!: (value: unknown) => void
    api.run.mockReturnValue(new Promise(done => { resolve = done }))
    const wrapper = mount(GlobalEnterpriseAssetRunner, { props: { items: [asset] } })
    ;(wrapper.vm as unknown as { choose: (item: typeof asset) => void }).choose(asset); await nextTick()
    await wrapper.get('#enterprise-asset-input').setValue('私密输入')
    await wrapper.get('[data-action="run-enterprise-public"]').trigger('click'); await flushPromises()
    const user = useUserStore(); user.token = 'token-2'; auth.token = 'token-2'
    user.profile = { id: 6, username: 'other', role: 'user', status: 1 }
    resolve({ ...result, output: '旧账号结果' }); await flushPromises()
    expect(wrapper.text()).not.toContain('旧账号结果'); expect(wrapper.find('#enterprise-asset-input').exists()).toBe(false)
    wrapper.unmount()
  })
})
