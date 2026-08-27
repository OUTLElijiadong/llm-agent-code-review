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

  it('概览成员徽章为静态状态展示,不制造无动作的焦点入口', async () => {
    teamApi.detail.mockReset().mockResolvedValue({
      ...team(53, '静态成员状态'),
      members: [{
        member_id: 1, member_key: 'scanner', display_name: '扫描员',
        address: 'agent:scanner', kind: 'runtime', role: 'worker', status: 'completed',
      }],
    })
    const wrapper = mount(AgentTeamSidePanel, {
      props: { teamId: 53 },
      global: { stubs: { Teleport: true, Transition: false, 'el-icon': true, Close: true } },
    })
    await flushPromises()

    const badge = wrapper.get('.team-member-badge')
    expect(badge.attributes('role')).toBeUndefined()
    expect(badge.attributes('tabindex')).toBeUndefined()
    expect(badge.classes()).not.toContain('is-interactive')
    wrapper.unmount()
  })

  it('打开后聚焦关闭按钮,支持 Esc 关闭并在关闭后恢复触发点焦点', async () => {
    teamApi.detail.mockReset().mockResolvedValue(team(54, '键盘可访问团队'))
    const launcher = document.createElement('button')
    launcher.textContent = '打开团队'
    document.body.appendChild(launcher)
    launcher.focus()
    const wrapper = mount(AgentTeamSidePanel, {
      props: { teamId: 54 },
      attachTo: document.body,
      global: { stubs: { Teleport: true, Transition: false, 'el-icon': true, Close: true } },
    })
    await flushPromises()

    expect(document.activeElement).toBe(wrapper.get('.panel-close').element)
    await wrapper.get('.team-side-panel').trigger('keydown', { key: 'Escape' })
    expect(wrapper.emitted('close')).toHaveLength(1)

    await wrapper.setProps({ teamId: null })
    await flushPromises()
    expect(document.activeElement).toBe(launcher)
    wrapper.unmount()
    launcher.remove()
  })

  it('页签暴露标准 tab 语义并同步选中状态', async () => {
    teamApi.detail.mockReset().mockResolvedValue(team(55, '页签语义团队'))
    const wrapper = mount(AgentTeamSidePanel, {
      props: { teamId: 55 },
      global: { stubs: { Teleport: true, Transition: false, 'el-icon': true, Close: true } },
    })
    await flushPromises()

    expect(wrapper.get('.panel-tabs').attributes('role')).toBe('tablist')
    const tabs = wrapper.findAll('.tab-btn')
    expect(tabs[0].attributes('role')).toBe('tab')
    expect(tabs[0].attributes('aria-selected')).toBe('true')
    expect(tabs[1].attributes('aria-selected')).toBe('false')

    await tabs[1].trigger('click')
    expect(tabs[0].attributes('aria-selected')).toBe('false')
    expect(tabs[1].attributes('aria-selected')).toBe('true')
    expect(wrapper.get('.tab-members').attributes('role')).toBe('tabpanel')
    wrapper.unmount()
  })
})
