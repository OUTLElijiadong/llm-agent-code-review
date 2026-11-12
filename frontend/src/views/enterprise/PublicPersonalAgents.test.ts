import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

const api = vi.hoisted(() => ({ list: vi.fn(), run: vi.fn(), byId: vi.fn(), byRequest: vi.fn() }))
const auth = vi.hoisted(() => ({ token: 'token-1' }))
vi.mock('@/api/personalPublications', () => ({ listPublicAgents: api.list, runPublicAgent: api.run,
  getPublicAgentRun: api.byId, getPublicAgentRunByRequest: api.byRequest }))
vi.mock('@/utils/token', () => ({ getToken: () => auth.token, setToken: vi.fn(), clearToken: vi.fn() }))
import { useUserStore } from '@/stores/user'
import PublicPersonalAgents from './PublicPersonalAgents.vue'

beforeEach(() => {
  sessionStorage.clear(); setActivePinia(createPinia()); auth.token = 'token-1'
  const user = useUserStore(); user.token = auth.token
  user.profile = { id: 5, username: 'member', role: 'user', status: 1 }
  Object.values(api).forEach(fn => fn.mockReset())
  api.list.mockResolvedValue([{ id: 3, title: '公开助手', version_number: 2, execution_available: true }])
  vi.stubGlobal('crypto', { randomUUID: () => 'run-key-1',
    subtle: { digest: async () => new Uint8Array([1, 2, 3]).buffer } })
})

describe('公开个人 Agent', () => {
  it('费用未知时保存请求标识，作品下架后仍可查询本人运行', async () => {
    api.run.mockResolvedValue({ id: 7, publication_id: 3, agent_id: null, version_number: 2,
      status: 'awaiting_reconciliation', output: null, actual_milli: null })
    let wrapper = mount(PublicPersonalAgents); await flushPromises()
    await wrapper.get('[data-publication="3"]').trigger('click')
    await wrapper.get('#public-agent-input').setValue('检查这个方案')
    await wrapper.get('[data-action="run-public"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('待核对')
    expect(wrapper.text()).not.toContain('0 积分')
    expect(api.run).toHaveBeenCalledWith(3, { request_key: 'run-key-1', input_text: '检查这个方案' }, 'token-1')
    wrapper.unmount()
    api.list.mockResolvedValue([])
    api.byId.mockResolvedValue({ id: 7, publication_id: 3, agent_id: null, version_number: 2,
      status: 'awaiting_reconciliation', output: null, actual_milli: null })
    wrapper = mount(PublicPersonalAgents); await flushPromises()
    await wrapper.get('[data-saved-publication="3"]').trigger('click'); await flushPromises()
    expect(api.byId).toHaveBeenCalledWith(3, 7, 'token-1')
    expect(wrapper.text()).toContain('待核对')
    wrapper.unmount()
  })

  it('响应未知时只允许原内容使用同一请求键重试', async () => {
    api.run.mockRejectedValueOnce(new Error('timeout')).mockResolvedValueOnce({ id: 9, publication_id: 3,
      agent_id: null, version_number: 2, status: 'succeeded', output: '完成', actual_milli: 100 })
    const wrapper = mount(PublicPersonalAgents); await flushPromises()
    await wrapper.get('[data-publication="3"]').trigger('click')
    await wrapper.get('#public-agent-input').setValue('原内容')
    await wrapper.get('[data-action="run-public"]').trigger('click'); await flushPromises()
    await wrapper.get('[data-action="run-public"]').trigger('click'); await flushPromises()
    expect(api.run.mock.calls.map(call => call[1].request_key)).toEqual(['run-key-1', 'run-key-1'])
    expect(wrapper.text()).toContain('完成')
    wrapper.unmount()
  })

  it('账号切换立即清除输入，并丢弃旧账号迟到的运行结果', async () => {
    let resolveRun!: (value: unknown) => void
    api.run.mockReturnValue(new Promise(resolve => { resolveRun = resolve }))
    const wrapper = mount(PublicPersonalAgents); await flushPromises()
    await wrapper.get('[data-publication="3"]').trigger('click')
    await wrapper.get('#public-agent-input').setValue('账号一的私密输入')
    await wrapper.get('[data-action="run-public"]').trigger('click'); await flushPromises()
    const user = useUserStore()
    user.token = 'token-2'
    await nextTick()
    expect(wrapper.find('[data-saved-publication="3"]').exists()).toBe(false)
    auth.token = 'token-2'
    user.profile = { id: 6, username: 'other', role: 'user', status: 1 }
    resolveRun({ id: 9, publication_id: 3, agent_id: null, version_number: 2,
      status: 'succeeded', output: '账号一的结果', actual_milli: 100 })
    await flushPromises()
    expect(wrapper.text()).not.toContain('账号一的结果')
    expect(wrapper.find('#public-agent-input').exists()).toBe(false)
    expect(wrapper.find('[data-saved-publication="3"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('新建运行保留历史记录，选择历史后仍可使用上架的作品', async () => {
    api.run.mockResolvedValue({ id: 9, publication_id: 3, agent_id: null, version_number: 2,
      status: 'succeeded', output: '完成', actual_milli: 100 })
    api.byId.mockResolvedValue({ id: 9, publication_id: 3, agent_id: null, version_number: 2,
      status: 'succeeded', output: '完成', actual_milli: 100 })
    const wrapper = mount(PublicPersonalAgents); await flushPromises()
    await wrapper.get('[data-publication="3"]').trigger('click')
    await wrapper.get('#public-agent-input').setValue('输入')
    await wrapper.get('[data-action="run-public"]').trigger('click'); await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '新建运行')!.trigger('click')
    expect(wrapper.find('[data-saved-publication="3"]').exists()).toBe(true)
    await wrapper.get('[data-saved-publication="3"]').trigger('click'); await flushPromises()
    expect(wrapper.findAll('button').some(button => button.text() === '新建运行')).toBe(true)
    wrapper.unmount()
  })
})
