import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ list: vi.fn(), detail: vi.fn() }))
const auth = vi.hoisted(() => ({ token: 'token-1' }))
vi.mock('@/api/agentRunHistory', () => ({ listAgentRuns: api.list, getAgentRun: api.detail }))
vi.mock('@/utils/token', () => ({ getToken: () => auth.token, setToken: vi.fn(), clearToken: vi.fn() }))
import { useUserStore } from '@/stores/user'
import AgentRunHistory from './AgentRunHistory.vue'

const row = { id: 7, agent_id: null, publication_id: 9, title: '已下架助手', version_number: 1,
  status: 'succeeded', actual_milli: 1200, created_at: '2026-11-08T10:00:00' }
beforeEach(() => {
  setActivePinia(createPinia()); auth.token = 'token-1'; api.list.mockReset(); api.detail.mockReset()
  const user = useUserStore(); user.profile = { id: 1, username: 'a', role: 'user', status: 1 }; user.token = auth.token
})
describe('运行记录', () => {
  it('在公开作品下架后按记录查询本人结果和费用', async () => {
    api.list.mockResolvedValue([row]); api.detail.mockResolvedValue({ ...row, output: '本人结果' })
    const wrapper = mount(AgentRunHistory, { props: { source: 'public' } })
    wrapper.get('details').element.open = true; await new Promise(resolve => setTimeout(resolve, 5)); await flushPromises()
    expect(wrapper.text()).toContain('已下架助手'); expect(wrapper.text()).toContain('1.2 积分')
    await wrapper.get('[data-run="7"]').trigger('click'); await flushPromises()
    expect(api.detail).toHaveBeenCalledWith(row, 'token-1'); expect(wrapper.text()).toContain('本人结果')
  })
  it('企业共享资产下架后以独立来源读取本人历史，不混淆个人作品 ID', async () => {
    const enterpriseRow = { ...row, agent_id: null, publication_id: null, enterprise_publication_id: 9 }
    api.list.mockResolvedValue([enterpriseRow])
    api.detail.mockResolvedValue({ id: 7, enterprise_publication_id: 9, version_number: 1,
      status: 'succeeded', output: '本人企业资产结果', result_reason: null, actual_milli: 1200 })
    const wrapper = mount(AgentRunHistory, { props: { source: 'enterprise_public' } })
    wrapper.get('details').element.open = true; await new Promise(resolve => setTimeout(resolve, 5)); await flushPromises()
    expect(api.list).toHaveBeenCalledWith('enterprise_public', 'token-1', undefined)
    await wrapper.get('[data-run="7"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('本人企业资产结果')
    wrapper.unmount()
  })
  it('历史详情返回其他企业发布 ID 时不显示结果', async () => {
    const enterpriseRow = { ...row, agent_id: null, publication_id: null, enterprise_publication_id: 9 }
    api.list.mockResolvedValue([enterpriseRow])
    api.detail.mockResolvedValue({ id: 7, enterprise_publication_id: 10, status: 'succeeded',
      output: '错误资产结果', actual_milli: 0 })
    const wrapper = mount(AgentRunHistory, { props: { source: 'enterprise_public' } })
    wrapper.get('details').element.open = true; await new Promise(resolve => setTimeout(resolve, 5)); await flushPromises()
    await wrapper.get('[data-run="7"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).not.toContain('错误资产结果'); expect(wrapper.get('[role=alert]').text()).toContain('结果加载失败')
    wrapper.unmount()
  })
  it('结果查询失败可重试原记录', async () => {
    api.list.mockResolvedValue([row]); api.detail.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce({ ...row, output: '恢复结果' })
    const wrapper = mount(AgentRunHistory, { props: { source: 'public' } })
    wrapper.get('details').element.open = true; await new Promise(resolve => setTimeout(resolve, 5)); await flushPromises()
    await wrapper.get('[data-run="7"]').trigger('click'); await flushPromises()
    await wrapper.get('[role=alert] button').trigger('click'); await flushPromises()
    expect(api.detail).toHaveBeenCalledTimes(2); expect(wrapper.text()).toContain('恢复结果')
  })
  it('分页加载记录', async () => {
    const first = Array.from({ length: 20 }, (_, i) => ({ ...row, id: i + 1 }))
    api.list.mockImplementation((_source, _token, beforeId) => Promise.resolve(beforeId ? [{ ...row, id: 21 }] : first))
    const wrapper = mount(AgentRunHistory, { props: { source: 'public' } })
    wrapper.get('details').element.open = true; await new Promise(resolve => setTimeout(resolve, 5)); await flushPromises()
    await wrapper.get('[data-action=more]').trigger('click'); await flushPromises()
    expect(api.list).toHaveBeenLastCalledWith('public', 'token-1', 20)
    expect(wrapper.find('[data-run="21"]').exists()).toBe(true)
  })
  it('加载下一页使用上一页末尾 ID，避免新运行插入造成重复', async () => {
    const first = Array.from({ length: 20 }, (_, i) => ({ ...row, id: 30 - i }))
    api.list.mockImplementation((_source, _token, beforeId) => Promise.resolve(beforeId === 11 ? [{ ...row, id: 10 }] : first))
    const wrapper = mount(AgentRunHistory, { props: { source: 'public' } })
    wrapper.get('details').element.open = true; await new Promise(resolve => setTimeout(resolve, 5)); await flushPromises()
    await wrapper.get('[data-action=more]').trigger('click'); await flushPromises()
    expect(api.list).toHaveBeenLastCalledWith('public', 'token-1', 11)
    expect(wrapper.findAll('[data-run="11"]')).toHaveLength(1)
    expect(wrapper.find('[data-run="10"]').exists()).toBe(true)
  })
  it('切换账号后丢弃旧详情', async () => {
    let resolve!: (value: unknown) => void
    api.list.mockResolvedValue([row]); api.detail.mockImplementation(() => new Promise(done => { resolve = done }))
    const wrapper = mount(AgentRunHistory, { props: { source: 'public' } })
    wrapper.get('details').element.open = true; await new Promise(resolve => setTimeout(resolve, 5)); await flushPromises()
    await wrapper.get('[data-run="7"]').trigger('click')
    const user = useUserStore(); auth.token = 'token-2'; user.token = 'token-2'; user.profile = { id: 2, username: 'b', role: 'user', status: 1 }
    resolve({ ...row, output: '旧账号结果' }); await flushPromises()
    expect(wrapper.text()).not.toContain('旧账号结果')
  })
  it('切换账号后丢弃旧请求且不展示旧结果', async () => {
    let resolve!: (value: unknown) => void
    api.list.mockImplementationOnce(() => new Promise(done => { resolve = done })).mockResolvedValue([])
    const wrapper = mount(AgentRunHistory, { props: { source: 'public' } })
    wrapper.get('details').element.open = true; await new Promise(resolve => setTimeout(resolve, 5)); await flushPromises()
    const user = useUserStore(); auth.token = 'token-2'; user.token = 'token-2'; user.profile = { id: 2, username: 'b', role: 'user', status: 1 }
    resolve([row]); await flushPromises()
    expect(wrapper.text()).not.toContain('已下架助手')
  })
})
