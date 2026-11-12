import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ list: vi.fn(), submit: vi.fn(), withdraw: vi.fn() }))
vi.mock('@/api/personalPublications', () => ({ listPersonalPublications: api.list,
  submitPersonalPublication: api.submit, withdrawPersonalPublication: api.withdraw }))
vi.mock('@/utils/token', () => ({ getToken: () => 'token-1' }))
import Panel from './PersonalPublicationPanel.vue'

beforeEach(() => {
  sessionStorage.clear()
  api.list.mockReset(); api.submit.mockReset(); api.withdraw.mockReset()
  api.list.mockResolvedValue([])
  vi.stubGlobal('crypto', { randomUUID: () => 'publication-key-1' })
})

describe('个人发布申请', () => {
  it('提交结果未知后刷新仍以原键和原版本重试', async () => {
    api.submit.mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce({
      id: 7, agent_id: 3, version_number: 2, title: '助手', status: 'pending',
    })
    let wrapper = mount(Panel, { props: { userId: 1, agentId: 3, version: 2, available: true } })
    await flushPromises(); await wrapper.get('[data-action="publish"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('结果未知')
    wrapper.unmount()
    wrapper = mount(Panel, { props: { userId: 1, agentId: 3, version: 3, available: true } })
    await flushPromises(); await wrapper.get('[data-action="publish"]').trigger('click'); await flushPromises()
    expect(api.submit).toHaveBeenLastCalledWith(3, { expected_version: 2, request_key: 'publication-key-1' }, 'token-1')
    wrapper.unmount()
  })

  it('只显示当前 Agent 的申请，待审申请可撤回', async () => {
    api.list.mockResolvedValue([{ id: 7, agent_id: 3, version_number: 2, title: '助手', status: 'pending' },
      { id: 8, agent_id: 4, version_number: 1, title: '其他', status: 'pending' }])
    api.withdraw.mockResolvedValue({})
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = mount(Panel, { props: { userId: 1, agentId: 3, version: 2, available: true } })
    await flushPromises()
    expect(wrapper.text()).not.toContain('其他')
    await wrapper.get('[data-withdraw="7"]').trigger('click'); await flushPromises()
    expect(api.withdraw).toHaveBeenCalledWith(7, 'token-1')
    wrapper.unmount()
  })
})
