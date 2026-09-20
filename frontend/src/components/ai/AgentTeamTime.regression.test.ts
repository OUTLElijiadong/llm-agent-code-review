import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentTeamDetail, AgentTeamMember } from '@/api/agentTeams'
import AgentMemberWorkCard from './AgentMemberWorkCard.vue'
import AgentTeamCard from './AgentTeamCard.vue'
import AgentTeamSidePanel from './AgentTeamSidePanel.vue'
import AgentTeamTrace from './AgentTeamTrace.vue'
import AgentTeamWindow from './AgentTeamWindow.vue'

const teamApi = vi.hoisted(() => ({ detail: vi.fn() }))
vi.mock('@/api/agentTeams', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/agentTeams')>(),
  getAgentTeam: teamApi.detail,
}))

const startedAt = '2026-09-20T12:50:00'
const member: AgentTeamMember = {
  member_id: 1, member_key: 'review', display_name: '正式代码审查',
  address: 'agent:review_orchestrator', kind: 'runtime', role: 'worker',
  status: 'running', started_at: startedAt,
}
const team: AgentTeamDetail = {
  team_id: 64, title: '并行审查', surface: 'user', session_id: 'timezone-test',
  status: 'running', max_active_children: 2, trace_id: 'timezone-test',
  created_at: startedAt, started_at: startedAt,
  members: [member], tasks: [],
  counts: { total: 1, completed: 0, running: 1, queued: 0, failed: 0, blocked: 0 },
  events: [{ event_id: 1, team_id: 64, member_id: 1, event_type: 'task.claimed', created_at: startedAt }],
  messages: [{
    message_id: 'msg-time', sent_from: 'agent:review_orchestrator', send_to: 'manager',
    message_type: 'task.result', subject: '审查进度', status: 'completed',
    trace_id: 'timezone-test', correlation_id: 'timezone-test', create_time: startedAt,
  }],
}
const global = { stubs: { Teleport: true, Transition: false, 'el-icon': true } }

enableAutoUnmount(afterEach)

describe('团队 UTC 时间回归', () => {
  beforeAll(() => vi.stubEnv('TZ', 'Asia/Shanghai'))
  afterAll(() => vi.unstubAllEnvs())
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-20T12:51:05Z'))
    // 必须在非 UTC 环境复现，不能让 CI 的 UTC 时区掩盖缺陷。
    expect(new Date().getTimezoneOffset()).toBe(-480)
    teamApi.detail.mockResolvedValue(team)
  })

  it.each(['member', 'team', 'event'] as const)('从 %s 取无后缀 UTC 开始时间时，计时与操作时间保持准确', async (source) => {
    const wrapper = mount(AgentMemberWorkCard, {
      props: {
        member: { ...member, started_at: source === 'member' ? startedAt : null },
        teamStartedAt: source === 'team' ? startedAt : null,
        events: team.events,
      },
    })
    expect(wrapper.get('.member-work-timing').text()).toBe('已工作 1分5秒')
    expect(wrapper.get('.member-work-log time').text()).toBe('20:50')
    await vi.advanceTimersByTimeAsync(1000)
    expect(wrapper.get('.member-work-timing').text()).toBe('已工作 1分6秒')
  })

  it('完成时间带偏移量、开始时间无后缀时，耗时仍为真实 7 秒', () => {
    const wrapper = mount(AgentMemberWorkCard, {
      props: { member: { ...member, status: 'completed', completed_at: '2026-09-20T20:50:07+08:00' } },
    })
    expect(wrapper.get('.member-work-timing').text()).toBe('用时 7秒')
  })

  it('缺失或错误时间不显示 NaN 耗时和操作时钟', () => {
    const wrapper = mount(AgentMemberWorkCard, {
      props: { member: { ...member, started_at: 'invalid' }, events: [{ ...team.events[0], created_at: 'invalid' }] },
    })
    expect(wrapper.find('.member-work-timing').exists()).toBe(false)
    expect(wrapper.find('.member-work-log time').exists()).toBe(false)
  })

  it('紧凑团队卡片的团队与成员运行时间一致', () => {
    const wrapper = mount(AgentTeamCard, { props: { team }, global })
    expect(wrapper.get('.team-card-duration').text()).toBe('已运行 1分5秒')
    expect(wrapper.text()).toContain('工作中 1分5秒')
  })

  it('侧面板的团队时长与事件时钟采用相同时区', async () => {
    const wrapper = mount(AgentTeamSidePanel, { props: { teamId: 64 }, global })
    await flushPromises()
    expect(wrapper.get('.meta-duration').text()).toBe('已运行 1分5秒')
    await wrapper.findAll('.tab-btn')[3].trigger('click')
    expect(wrapper.text()).toContain('20:50:00')
  })

  it('协作过程中的事件和消息显示本地时钟', async () => {
    const wrapper = mount(AgentTeamTrace, { props: { team } })
    await wrapper.get('.agent-team-toggle').trigger('click')
    expect(wrapper.get('.agent-team-event time').text()).toBe('20:50')
    expect(wrapper.get('.agent-team-message time').text()).toBe('20:50')
  })

  it('独立团队窗口的消息与成员操作时钟一致', () => {
    const wrapper = mount(AgentTeamWindow, { props: { visible: true, team }, global })
    expect(wrapper.get('.team-chat-time').text()).toBe('20:50')
    expect(wrapper.get('.member-work-log time').text()).toBe('20:50')
  })
})
