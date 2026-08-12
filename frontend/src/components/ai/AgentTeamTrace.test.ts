import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

const teamApi = vi.hoisted(() => ({ messages: vi.fn() }))
vi.mock('@/api/agentTeams', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/agentTeams')>()
  return { ...actual, listAgentTeamMessages: teamApi.messages }
})

import AgentTeamTrace from './AgentTeamTrace.vue'
const team = {
  team_id: 42,
  title: '发布前验证',
  objective: '并行执行测试',
  surface: 'user' as const,
  session_id: 'session-001',
  status: 'running' as const,
  max_active_children: 3,
  trace_id: 'trace-team-42',
  counts: { total: 2, completed: 1, running: 1, queued: 0, failed: 0, blocked: 0 },
  members: [
    { member_id: 1, member_key: 'reader', display_name: '读取 Agent', address: 'agent:project_analyzer', kind: 'runtime', role: 'worker' as const, status: 'completed' },
    { member_id: 2, member_key: 'reviewer', display_name: '验证 Agent', address: 'agent:code_reviewer', kind: 'runtime', role: 'verifier' as const, status: 'running' },
  ],
  tasks: [
    { task_id: 11, task_key: 'read', member_id: 1, title: '读取项目', depends_on: [], status: 'completed' as const, priority: 0, attempt_count: 1, max_attempts: 3 },
    { task_id: 12, task_key: 'verify', member_id: 2, member_key: 'reviewer', title: '验证结果', depends_on: ['read'], status: 'running' as const, priority: 0, attempt_count: 1, max_attempts: 3 },
  ],
  events: [{ event_id: 1, team_id: 42, event_type: 'task.completed', trace_id: 'trace-team-42', created_at: '2026-08-12T12:00:00Z' }],
  messages: [{ message_id: 'msg-1', trace_id: 'trace-team-42', correlation_id: 'corr-1', sent_from: 'agent:project_analyzer', send_to: 'agent:code_reviewer', message_type: 'task.result', subject: '读取完成', status: 'completed', create_time: '2026-08-12T12:00:00Z' }],
}

describe('AgentTeamTrace', () => {
  it('默认折叠，展开后显示脱敏团队树、依赖和消息', async () => {
    const wrapper = mount(AgentTeamTrace, { props: { team } })

    expect(wrapper.find('.agent-team-trace-body').exists()).toBe(false)
    expect(wrapper.text()).toContain('发布前验证')
    await wrapper.find('.agent-team-toggle').trigger('click')

    expect(wrapper.find('.agent-team-trace-body').exists()).toBe(true)
    expect(wrapper.text()).toContain('读取 Agent')
    expect(wrapper.text()).toContain('依赖: read')
    expect(wrapper.text()).toContain('读取完成')
    expect(wrapper.text()).not.toContain('并行执行测试')
    expect(wrapper.find('.agent-team-toggle').attributes('aria-expanded')).toBe('true')
  })

  it('在窄屏内容中保持可换行且显示错误状态', () => {
    const wrapper = mount(AgentTeamTrace, { props: { team, error: '同步暂时中断' } })
    expect(wrapper.find('.agent-team-error').text()).toBe('同步暂时中断')
    expect(wrapper.find('.agent-team-trace').attributes('aria-label')).toContain('协作团队')
  })

  it('按分段提供任务、事件和消息的完整链路，并可收起为初始视图', async () => {
    const longTeam = {
      ...team,
      tasks: Array.from({ length: 15 }, (_, index) => ({
        task_id: index + 1,
        task_key: `task-${index + 1}`,
        member_id: 1,
        title: `任务 ${index + 1}`,
        depends_on: [],
        status: 'completed' as const,
        priority: 0,
        attempt_count: 1,
        max_attempts: 3,
      })),
      events: Array.from({ length: 14 }, (_, index) => ({
        event_id: index + 1,
        team_id: 42,
        event_type: `事件条目#${String(index + 1).padStart(2, '0')}`,
        trace_id: 'trace-team-42',
        created_at: '2026-08-12T12:00:00Z',
      })),
      messages: Array.from({ length: 13 }, (_, index) => ({
        message_id: `msg-${index + 1}`,
        trace_id: 'trace-team-42',
        correlation_id: `corr-${index + 1}`,
        sent_from: 'agent:project_analyzer',
        send_to: 'agent:code_reviewer',
        message_type: 'task.result',
        subject: `消息条目#${String(index + 1).padStart(2, '0')}`,
        status: 'completed',
        create_time: '2026-08-12T12:00:00Z',
      })),
    }
    const wrapper = mount(AgentTeamTrace, { props: { team: longTeam } })

    await wrapper.find('.agent-team-toggle').trigger('click')

    expect(wrapper.findAll('.agent-team-task')).toHaveLength(12)
    expect(wrapper.findAll('.agent-team-event')).toHaveLength(12)
    expect(wrapper.findAll('.agent-team-message')).toHaveLength(12)
    expect(wrapper.findAll('.agent-team-record-viewport')).toHaveLength(3)
    expect(wrapper.text()).not.toContain('任务 15')
    expect(wrapper.text()).not.toContain('事件条目#01')
    expect(wrapper.text()).not.toContain('消息条目#01')

    await wrapper.find('[aria-label="查看更多任务"]').trigger('click')
    await wrapper.find('[aria-label="查看更多早期事件"]').trigger('click')
    await wrapper.find('[aria-label="查看更多早期消息"]').trigger('click')

    expect(wrapper.findAll('.agent-team-task')).toHaveLength(15)
    expect(wrapper.findAll('.agent-team-event')).toHaveLength(14)
    expect(wrapper.findAll('.agent-team-message')).toHaveLength(13)
    expect(wrapper.text()).toContain('任务 15')
    expect(wrapper.text()).toContain('事件条目#01')
    expect(wrapper.text()).toContain('消息条目#01')

    await wrapper.find('[aria-label="收起任务"]').trigger('click')
    await wrapper.find('[aria-label="收起事件"]').trigger('click')
    await wrapper.find('[aria-label="收起消息"]').trigger('click')

    expect(wrapper.findAll('.agent-team-task')).toHaveLength(12)
    expect(wrapper.findAll('.agent-team-event')).toHaveLength(12)
    expect(wrapper.findAll('.agent-team-message')).toHaveLength(12)
  })

  it('记录数量不超过首段时不显示分页控制', async () => {
    const wrapper = mount(AgentTeamTrace, { props: { team } })

    await wrapper.find('.agent-team-toggle').trigger('click')

    expect(wrapper.find('.agent-team-pager').exists()).toBe(false)
  })

  it('将完整结构化证据放在按需详情中，不截断可追溯内容', async () => {
    const tail = 'evidence-tail-should-remain-visible'
    const longDetail = `${'x'.repeat(800)}${tail}`
    const detailedTeam = {
      ...team,
      tasks: [{ ...team.tasks[0], result: { diagnostic: longDetail } }],
      events: [{ ...team.events[0], detail: { diagnostic: longDetail } }],
      messages: [{ ...team.messages[0], payload: { diagnostic: longDetail } }],
    }
    const wrapper = mount(AgentTeamTrace, { props: { team: detailedTeam } })

    await wrapper.find('.agent-team-toggle').trigger('click')

    expect(wrapper.findAll('.agent-team-record-detail')).toHaveLength(2)
    expect(wrapper.text()).toContain(tail)
  })

  it('本地最新页耗尽后按账本游标加载更早消息', async () => {
    const pagedTeam = {
      ...team,
      messages: Array.from({ length: 12 }, (_, index) => ({
        ...team.messages[0],
        ledger_id: index + 3,
        message_id: `msg-${index + 3}`,
        subject: `最新消息 ${index + 3}`,
      })),
      message_page: { total: 14, has_more: true, next_before_id: 3, page_size: 12 },
    }
    teamApi.messages.mockResolvedValue({
      items: [
        { ...team.messages[0], ledger_id: 1, message_id: 'msg-1', subject: '最早消息 1' },
        { ...team.messages[0], ledger_id: 2, message_id: 'msg-2', subject: '最早消息 2' },
      ],
      total: 14,
      has_more: false,
      next_before_id: null,
      page_size: 500,
    })
    const wrapper = mount(AgentTeamTrace, { props: { team: pagedTeam } })

    await wrapper.find('.agent-team-toggle').trigger('click')
    expect(wrapper.text()).not.toContain('最早消息 1')
    await wrapper.find('[aria-label="查看更多早期消息"]').trigger('click')
    await flushPromises()

    expect(teamApi.messages).toHaveBeenCalledWith(42, 3)
    expect(wrapper.text()).toContain('最早消息 1')
    expect(wrapper.findAll('.agent-team-message')).toHaveLength(14)
  })
})
