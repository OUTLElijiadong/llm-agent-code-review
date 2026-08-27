import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

const teamApi = vi.hoisted(() => ({ detail: vi.fn() }))
vi.mock('@/api/agentTeams', () => ({ getAgentTeam: teamApi.detail }))

import AgentTeamSidePanel from './AgentTeamSidePanel.vue'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

function team(teamId: number, title: string) {
  return {
    team_id: teamId, title, surface: 'user', session_id: 'session-test', status: 'running',
    max_active_children: 2, trace_id: `trace-${teamId}`,
    counts: { total: 0, completed: 0, running: 0, queued: 0, failed: 0, blocked: 0 },
    members: [], tasks: [], events: [], messages: [],
  }
}

describe('AgentTeamSidePanel', () => {
  it('切换团队后迟到的旧请求不能覆盖当前团队', async () => {
    const first = deferred<any>()
    const second = deferred<any>()
    teamApi.detail.mockReset().mockImplementation((teamId: number) => teamId === 1 ? first.promise : second.promise)
    const wrapper = mount(AgentTeamSidePanel, {
      props: { teamId: 1 },
      global: { stubs: { Teleport: true, Transition: false, 'el-icon': true, Close: true } },
    })

    await wrapper.setProps({ teamId: 2 })
    second.resolve(team(2, '当前团队'))
    await flushPromises()
    first.resolve(team(1, '过期团队'))
    await flushPromises()

    expect(wrapper.text()).toContain('当前团队')
    expect(wrapper.text()).not.toContain('过期团队')
    wrapper.unmount()
  })

  it('追问成员时携带当前团队编号和成员地址', async () => {
    teamApi.detail.mockReset().mockResolvedValue({
      ...team(52, '只读核验'),
      members: [{
        member_id: 1, member_key: 'security', display_name: '安全哨兵',
        address: 'agent:security_sentinel', kind: 'runtime', role: 'worker', status: 'running',
      }],
    })
    const wrapper = mount(AgentTeamSidePanel, {
      props: { teamId: 52 },
      global: { stubs: { Teleport: true, Transition: false, 'el-icon': true, Close: true } },
    })
    await flushPromises()
    await wrapper.findAll('.tab-btn')[1].trigger('click')
    await wrapper.find('.member-work-ask').trigger('click')

    expect(wrapper.emitted('ask-member')).toEqual([[
      { teamId: 52, name: '安全哨兵', address: 'agent:security_sentinel' },
    ]])
    wrapper.unmount()
  })
})
