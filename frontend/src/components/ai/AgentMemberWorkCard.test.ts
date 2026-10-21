import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import AgentMemberWorkCard from './AgentMemberWorkCard.vue'
import type { AgentTeamMember, AgentTeamTask } from '@/api/agentTeams'

const baseMember: AgentTeamMember = {
  member_id: 1,
  member_key: 'reader',
  display_name: '读取 Agent',
  address: 'agent:project_analyzer',
  kind: 'runtime',
  role: 'worker',
  status: 'running',
  started_at: '2026-08-12T11:58:19Z',
}

const tasks: AgentTeamTask[] = [
  { task_id: 11, task_key: 'read', member_id: 1, member_key: 'reader', title: '读取项目源码', depends_on: [], status: 'running', priority: 0, attempt_count: 1, max_attempts: 3 },
  { task_id: 12, task_key: 'index', member_id: 1, member_key: 'reader', title: '建立索引', depends_on: ['read'], status: 'queued', priority: 0, attempt_count: 0, max_attempts: 3 },
]

describe('AgentMemberWorkCard', () => {
  it('运行中成员展示当前任务、角色徽标与实时计时', () => {
    const wrapper = mount(AgentMemberWorkCard, {
      props: { member: baseMember, tasks },
    })
    expect(wrapper.find('.member-work-name').text()).toBe('读取 Agent')
    expect(wrapper.find('.member-work-role').text()).toBe('执行')
    // 状态行:正在做 running 任务
    expect(wrapper.find('.member-work-status-text').text()).toContain('读取项目源码')
    // 实时计时已工作(以「已工作」开头)
    expect(wrapper.find('.member-work-timing').text()).toMatch(/已工作 .+/)
    // 运行中有呼吸点,追问按钮默认隐藏
    expect(wrapper.find('.member-work-pulse').exists()).toBe(true)
    expect(wrapper.find('.member-work-ask').exists()).toBe(false)
  })

  it('无 running 任务时状态退化为思考中,事件日志兜底为任务摘要', () => {
    const wrapper = mount(AgentMemberWorkCard, {
      props: {
        member: { ...baseMember, status: 'running', started_at: null },
        tasks: [{ ...tasks[0], status: 'completed' }],
        teamStartedAt: '2026-08-12T11:58:19Z',
      },
    })
    expect(wrapper.find('.member-work-status-text').text()).toBe('思考中…')
    // 事件缺失时兜底日志:完成数摘要,不列全量标题
    expect(wrapper.text()).toContain('已完成 1 项任务')
    expect(wrapper.text()).not.toContain('读取项目源码')
  })

  it('事件日志按成员过滤并翻译为中文动作', () => {
    const wrapper = mount(AgentMemberWorkCard, {
      props: {
        member: baseMember,
        tasks: [],
        events: [
          { event_id: 1, team_id: 42, member_id: 1, event_type: 'task.claimed', created_at: '2026-08-12T11:58:00Z', detail: { task_key: 'read' } },
          { event_id: 2, team_id: 42, member_id: 1, event_type: 'task.completed', created_at: '2026-08-12T11:59:00Z' },
          { event_id: 3, team_id: 42, member_id: 2, event_type: 'task.failed', created_at: '2026-08-12T12:00:00Z' },
        ],
      },
    })
    const lines = wrapper.findAll('.member-work-log li')
    expect(lines).toHaveLength(2)
    expect(lines[0].text()).toContain('开始工作 read')
    expect(lines[1].text()).toContain('完成')
    // 其他成员的事件不进入本卡日志
    expect(wrapper.text()).not.toContain('失败')
  })

  it('showAsk 时渲染追问按钮并发出 ask 事件', async () => {
    const wrapper = mount(AgentMemberWorkCard, {
      props: { member: baseMember, tasks, showAsk: true },
    })
    const button = wrapper.find('.member-work-ask')
    expect(button.exists()).toBe(true)
    await button.trigger('click')
    expect(wrapper.emitted('ask')?.[0]?.[0]).toMatchObject({ member_id: 1 })
  })

  it('已完成成员展示终态与总耗时,不启动呼吸点', () => {
    const wrapper = mount(AgentMemberWorkCard, {
      props: {
        member: { ...baseMember, status: 'completed', started_at: '2026-08-12T11:58:19Z', completed_at: '2026-08-12T12:00:00Z' },
        tasks: [],
      },
    })
    expect(wrapper.find('.member-work-status-text').text()).toBe('已完成')
    expect(wrapper.find('.member-work-timing').text()).toMatch(/用时 .+/)
    expect(wrapper.find('.member-work-pulse').exists()).toBe(false)
  })
})

describe('AgentMemberWorkCard 派发任务可视化', () => {
  it('展示小菱派给成员的未完成任务、派发内容与成员进度，并可停止单个任务', async () => {
    const briefed: AgentTeamTask[] = [
      { ...tasks[0], instructions: '只读分析 src/ 下的鉴权代码，列出风险点' },
      tasks[1],
    ]
    const wrapper = mount(AgentMemberWorkCard, {
      props: { member: baseMember, tasks: briefed, showStop: true },
    })
    expect(wrapper.findAll('.member-work-task')).toHaveLength(2)
    expect(wrapper.find('.member-work-task-brief').text()).toContain('只读分析 src/ 下的鉴权代码')
    expect(wrapper.find('.member-work-progress').text()).toBe('完成 0/2')
    await wrapper.find('.member-work-stop').trigger('click')
    expect(wrapper.emitted('stop')?.[0]?.[0]).toMatchObject({ task_key: 'read' })
  })

  it('未开启 showStop 时不显示停止按钮', () => {
    const wrapper = mount(AgentMemberWorkCard, { props: { member: baseMember, tasks } })
    expect(wrapper.find('.member-work-stop').exists()).toBe(false)
  })
})
