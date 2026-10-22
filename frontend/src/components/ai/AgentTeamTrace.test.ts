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
    { task_id: 11, task_key: 'read', member_id: 1, title: '读取项目', instructions: '检查项目结构与现有文档', depends_on: [], status: 'completed' as const, priority: 0, attempt_count: 1, max_attempts: 3 },
    { task_id: 12, task_key: 'verify', member_id: 2, member_key: 'reviewer', title: '验证结果', instructions: '独立复核执行节点的证据', depends_on: ['read'], status: 'running' as const, priority: 0, attempt_count: 1, max_attempts: 3 },
  ],
  events: [{ event_id: 1, team_id: 42, event_type: 'task.completed', trace_id: 'trace-team-42', created_at: '2026-08-12T12:00:00Z' }],
  messages: [{ message_id: 'msg-1', trace_id: 'trace-team-42', correlation_id: 'corr-1', sent_from: 'agent:project_analyzer', send_to: 'agent:code_reviewer', message_type: 'task.result', subject: '读取完成', status: 'completed', create_time: '2026-08-12T12:00:00Z' }],
}

describe('AgentTeamTrace', () => {
  it('折叠按钮名称包含各自团队标题且控制面板 ID 唯一', () => {
    const first = mount(AgentTeamTrace, { props: { team } })
    const second = mount(AgentTeamTrace, { props: { team: { ...team, team_id: 43, title: '安全复核' } } })
    const sameTitle = mount(AgentTeamTrace, { props: { team: { ...team, team_id: 44 } } })
    const firstToggle = first.get('.agent-team-toggle')
    const secondToggle = second.get('.agent-team-toggle')
    const sameTitleToggle = sameTitle.get('.agent-team-toggle')

    expect(firstToggle.attributes('aria-label')).toContain('发布前验证')
    expect(secondToggle.attributes('aria-label')).toContain('安全复核')
    expect(firstToggle.attributes('aria-label')).toContain('团队编号 42')
    expect(sameTitleToggle.attributes('aria-label')).toContain('团队编号 44')
    expect(firstToggle.attributes('aria-label')).not.toBe(sameTitleToggle.attributes('aria-label'))
    expect(firstToggle.attributes('aria-controls')).not.toBe(secondToggle.attributes('aria-controls'))
  })

  it('默认折叠，展开后显示脱敏团队树、依赖和消息', async () => {
    const wrapper = mount(AgentTeamTrace, { props: { team } })

    expect(wrapper.find('.agent-team-trace-body').exists()).toBe(false)
    expect(wrapper.find('.agent-team-stats').exists()).toBe(false)
    expect(wrapper.text()).toContain('发布前验证')
    const toggle = wrapper.get('.agent-team-toggle')
    const traceId = toggle.attributes('aria-controls')
    expect(traceId).toBeTruthy()
    expect(wrapper.get(`#${traceId}`).classes()).toContain('agent-team-trace-panel')
    expect(wrapper.find('.agent-team-trace-body').exists()).toBe(false)
    expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBe('50')
    expect(wrapper.get('.agent-team-progress-label').text()).toContain('50%')
    expect(wrapper.get('.agent-team-progress-count').text()).toContain('1/2')
    await wrapper.find('.agent-team-toggle').trigger('click')

    expect(wrapper.find('.agent-team-trace-body').exists()).toBe(true)
    expect(wrapper.get(`#${traceId}`).attributes('id')).toBe(traceId)
    expect(wrapper.text()).toContain('读取 Agent')
    expect(wrapper.text()).toContain('依赖: read')
    expect(wrapper.text()).toContain('小菱派发内容')
    expect(wrapper.text()).toContain('检查项目结构与现有文档')
    expect(wrapper.text()).toContain('独立复核执行节点的证据')
    expect(wrapper.text()).toContain('读取完成')
    expect(wrapper.text()).not.toContain('并行执行测试')
    expect(wrapper.find('.agent-team-toggle').attributes('aria-expanded')).toBe('true')
  })

  it('折叠摘要保留失败与阻塞计数；总任务为零时不伪造百分比', () => {
    const interruptedTeam = {
      ...team,
      counts: { total: 3, completed: 1, running: 0, queued: 0, failed: 1, blocked: 1 },
    }
    const wrapper = mount(AgentTeamTrace, { props: { team: interruptedTeam } })
    expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBe('33')
    expect(wrapper.find('.agent-team-progress-problems').text()).toContain('失败/阻塞 2')

    const emptyTeam = { ...team, counts: { total: 0, completed: 0, running: 0, queued: 0, failed: 0, blocked: 0 } }
    const emptyWrapper = mount(AgentTeamTrace, { props: { team: emptyTeam } })
    expect(emptyWrapper.find('[role="progressbar"]').exists()).toBe(false)
    expect(emptyWrapper.text()).not.toContain('%')
  })

  it('只按真实 depends_on 绘制依赖边，并标出无前置任务的根节点', async () => {
    const wrapper = mount(AgentTeamTrace, { props: { team } })
    await wrapper.get('.agent-team-toggle').trigger('click')

    const edges = wrapper.findAll('[data-dependency-edge]')
    expect(edges).toHaveLength(1)
    expect(edges[0].attributes('data-dependency-edge')).toBe('read->verify')
    expect(wrapper.find('[data-task-root="read"]').text()).toContain('根节点')
    expect(wrapper.find('.agent-team-dependency-map').attributes('role')).toBe('img')
  })

  it('键盘可聚焦根节点追溯按钮，并仅高亮沿真实依赖边可达的后续任务', async () => {
    const tracedTeam = {
      ...team,
      tasks: [
        ...team.tasks,
        { ...team.tasks[1], task_id: 13, task_key: 'publish', title: '发布结果', depends_on: ['verify'] },
        { ...team.tasks[0], task_id: 14, task_key: 'metrics', title: '独立指标', depends_on: [] },
      ],
    }
    const wrapper = mount(AgentTeamTrace, { props: { team: tracedTeam }, attachTo: document.body })
    await wrapper.get('.agent-team-toggle').trigger('click')
    const traceButton = wrapper.get('[aria-label="从根节点 读取项目 追溯后续任务"]')

    expect(traceButton.element.tagName).toBe('BUTTON')
    ;(traceButton.element as HTMLButtonElement).focus()
    expect(document.activeElement).toBe(traceButton.element)
    expect(traceButton.attributes('aria-pressed')).toBe('false')
    await traceButton.trigger('click')

    expect(traceButton.attributes('aria-pressed')).toBe('true')
    expect(wrapper.get('[data-task-node="read"]').classes()).toContain('is-traced')
    expect(wrapper.get('[data-task-node="verify"]').classes()).toContain('is-traced')
    expect(wrapper.get('[data-task-node="publish"]').classes()).toContain('is-traced')
    expect(wrapper.get('[data-task-node="metrics"]').classes()).toContain('is-dimmed')
    expect(wrapper.get('[data-dependency-edge="read->verify"]').classes()).toContain('is-traced')
    expect(wrapper.get('[data-dependency-edge="verify->publish"]').classes()).toContain('is-traced')
    expect(wrapper.findAll('[data-dependency-edge="read->publish"]')).toHaveLength(0)
    expect(wrapper.find('[aria-label="清除根节点追溯"]').exists()).toBe(true)

    await wrapper.get('[aria-label="清除根节点追溯"]').trigger('click')
    expect(wrapper.get('[data-task-node="metrics"]').classes()).not.toContain('is-dimmed')
    wrapper.unmount()
    document.body.innerHTML = ''
  })

  it('依赖星图连线先于节点按序显现，逐步动效不改变真实边', async () => {
    const wrapper = mount(AgentTeamTrace, { props: { team } })
    await wrapper.get('.agent-team-toggle').trigger('click')
    const edge = wrapper.get('[data-dependency-edge="read->verify"]')
    const root = wrapper.get('[data-task-node="read"]')

    expect(edge.attributes('style')).toContain('--trace-delay: 0ms')
    expect(root.attributes('style')).toContain('--trace-delay: 32ms')
    expect(edge.classes()).toContain('agent-team-dependency-edge')
    expect(root.classes()).toContain('agent-team-dependency-node')
  })

  it('依赖链无法由 depends_on 完整读取时显示空态且不猜边', async () => {
    const unreadableTeam = {
      ...team,
      tasks: [{ ...team.tasks[0], depends_on: ['missing-key'] }],
    }
    const wrapper = mount(AgentTeamTrace, { props: { team: unreadableTeam } })
    await wrapper.get('.agent-team-toggle').trigger('click')

    expect(wrapper.find('.agent-team-dependency-map').exists()).toBe(false)
    expect(wrapper.find('[role="status"].agent-team-dependency-empty').text()).toContain('依赖链路暂不可读')
    expect(wrapper.findAll('[data-dependency-edge]')).toHaveLength(0)
  })

  it('展开控件有键盘焦点语义，内容在卡片流内展开并让后续内容位移', async () => {
    const wrapper = mount(AgentTeamTrace, { props: { team }, attachTo: document.body })
    const toggle = wrapper.get('.agent-team-toggle')
    expect(toggle.element.tagName).toBe('BUTTON')
    const panelId = toggle.attributes('aria-controls')
    expect(panelId).toBeTruthy()
    ;(toggle.element as HTMLButtonElement).focus()
    expect(document.activeElement).toBe(toggle.element)
    await toggle.trigger('click')

    expect(wrapper.get(`#${panelId}`).classes()).toContain('agent-team-trace-panel')
    expect(wrapper.get('.agent-team-trace-body').attributes('data-layout')).toBe('in-flow')
    const panel = wrapper.get('.agent-team-trace-panel')
    expect(wrapper.get('.agent-team-trace-body').element.parentElement?.parentElement).toBe(panel.element)
    expect(panel.element.parentElement).toBe(wrapper.get('.agent-team-trace').element)
    wrapper.unmount()
    document.body.innerHTML = ''
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

    const details = wrapper.findAll('.agent-team-record-detail')
    expect(details).toHaveLength(2)
    expect(details.map(detail => detail.get('summary').text())).toEqual(['查看事件详情', '查看消息详情'])
    for (const detail of details) {
      expect(detail.attributes('open')).toBeUndefined()
      expect(detail.get('summary').text()).not.toContain('diagnostic')
      expect(detail.get('code').text()).toContain(longDetail)
    }
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
