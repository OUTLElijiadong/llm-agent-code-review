import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import type { AgentTeamDetail } from '@/api/agentTeams'
import AgentTeamLiveGraph from './AgentTeamLiveGraph.vue'

function team(): AgentTeamDetail {
  return {
    team_id: 7, title: '代码质量与安全', surface: 'user', session_id: 'session-a',
    status: 'running', max_active_children: 2, trace_id: 'trace-a',
    members: [
      { member_id: 1, member_key: 'security', display_name: '安全检查', address: 'agent:security', kind: 'registered', role: 'worker', status: 'queued' },
      { member_id: 2, member_key: 'review', display_name: '临时审查员', address: 'temporary:review', kind: 'temporary', role: 'worker', status: 'created' },
    ],
    tasks: [{ task_id: 9, task_key: 'scan', member_id: 1, title: '检查输入边界', depends_on: [], status: 'queued', priority: 1, attempt_count: 0, max_attempts: 1 }],
    events: [], messages: [],
  }
}

describe('实时团队星图', () => {
  it('组队请求开始即展示等待真实账本，不编造成员', () => {
    const wrapper = mount(AgentTeamLiveGraph, { props: { teams: [], forming: true } })
    expect(wrapper.text()).toContain('正在组建团队')
    expect(wrapper.text()).toContain('等待服务端确认成员')
    expect(wrapper.findAll('[data-member-id]')).toHaveLength(0)
  })
  it('存在历史终态团队时仍明确标记新团队正在组建', () => {
    const previous = team()
    previous.status = 'completed'
    previous.tasks[0]!.status = 'completed'
    const wrapper = mount(AgentTeamLiveGraph, { props: { teams: [previous], forming: true } })

    expect(wrapper.text()).toContain('正在组建新团队')
    expect(wrapper.text()).toContain('新成员以服务端账本确认为准')
    expect(wrapper.text()).toContain('2 个子 Agent')
    expect(wrapper.findAll('[data-member-id]')).toHaveLength(2)
  })
  it('立即展示注册和临时成员，并随任务状态更新', async () => {
    const detail = team()
    const wrapper = mount(AgentTeamLiveGraph, { props: { teams: [detail] } })
    expect(wrapper.text()).toContain('2 个子 Agent')
    expect(wrapper.findAll('[data-member-id]')).toHaveLength(2)
    expect(wrapper.text()).toContain('临时审查员')
    expect(wrapper.text()).toContain('检查输入边界')
    expect(wrapper.text()).toContain('等待派发')
    const updated = { ...detail, tasks: [{ ...detail.tasks[0]!, status: 'running' as const }] }
    await wrapper.setProps({ teams: [updated] })
    expect(wrapper.find('[data-member-id="1"]').attributes('data-status')).toBe('running')
    expect(wrapper.text()).toContain('执行中')
    await wrapper.setProps({ teams: [{ ...updated, status: 'blocked', tasks: [{ ...updated.tasks[0]!, status: 'blocked' }] }] })
    expect(wrapper.find('[data-member-id="1"]').attributes('data-status')).toBe('blocked')
    expect(wrapper.text()).toContain('已阻塞')
  })
  it('团队取消或过期后停止活动展示', () => {
    for (const status of ['cancelled', 'expired'] as const) {
      const detail = team()
      detail.status = status
      detail.tasks[0]!.status = 'running'
      const wrapper = mount(AgentTeamLiveGraph, { props: { teams: [detail] } })
      expect(wrapper.find('[data-member-id="1"]').attributes('data-status')).toBe(status)
      expect(wrapper.find('.is-running').exists()).toBe(false)
    }
  })
  it('团队完成不能将从未派发的成员或失败任务标成完成', () => {
    const detail = team()
    detail.status = 'completed'
    detail.tasks[0]!.status = 'dead_letter'
    const wrapper = mount(AgentTeamLiveGraph, { props: { teams: [detail] } })
    expect(wrapper.find('[data-member-id="1"]').attributes('data-status')).toBe('dead_letter')
    expect(wrapper.find('[data-member-id="2"]').attributes('data-status')).toBe('created')
    expect(wrapper.findAll('.is-completed')).toHaveLength(0)
  })
  it('团队终态下未确认完成的活动任务停止动画并保留未完成语义', () => {
    const detail = team()
    detail.status = 'completed'
    detail.tasks[0]!.status = 'running'
    const wrapper = mount(AgentTeamLiveGraph, { props: { teams: [detail] } })
    expect(wrapper.find('[data-member-id="1"]').attributes('data-status')).toBe('stopped')
    expect(wrapper.text()).toContain('未确认完成')
    expect(wrapper.find('.is-running').exists()).toBe(false)
    expect(wrapper.find('.is-completed').exists()).toBe(false)
  })

})
