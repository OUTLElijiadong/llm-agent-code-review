import { enableAutoUnmount, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentTeamDetail, AgentTeamMember } from '@/api/agentTeams'
import AgentMemberWorkCard from './AgentMemberWorkCard.vue'
import AgentTeamCard from './AgentTeamCard.vue'
import AgentTeamTrace from './AgentTeamTrace.vue'

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
    setActivePinia(createPinia())
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-20T12:51:05Z'))
    // 必须在非 UTC 环境复现，不能让 CI 的 UTC 时区掩盖缺陷。
    expect(new Date().getTimezoneOffset()).toBe(-480)
    teamApi.detail.mockResolvedValue(team)
  })

  it('成员工作卡片不再展示耗时与事件时钟，避免时区偏差暴露给用户', async () => {
    const wrapper = mount(AgentMemberWorkCard, {
      props: { member, teamStartedAt: startedAt, events: team.events },
    })
    expect(wrapper.find('.member-work-timing').exists()).toBe(false)
    expect(wrapper.find('.member-work-log time').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('NaN')
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

  it('协作过程中的事件和消息显示本地时钟', async () => {
    const wrapper = mount(AgentTeamTrace, { props: { team } })
    await wrapper.get('.agent-team-toggle').trigger('click')
    expect(wrapper.get('.agent-team-event time').text()).toBe('20:50')
    expect(wrapper.get('.agent-team-message time').text()).toBe('20:50')
  })
})
