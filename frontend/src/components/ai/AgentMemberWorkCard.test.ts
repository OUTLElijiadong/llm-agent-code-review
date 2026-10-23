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
  it('展示成员、角色、当前任务、状态和计数，不显示耗时或事件正文', () => {
    const wrapper = mount(AgentMemberWorkCard, {
      props: {
        member: baseMember,
        tasks,
        events: [{
          event_id: 1,
          team_id: 42,
          member_id: 1,
          event_type: 'task.claimed',
          created_at: '2026-08-12T11:58:00Z',
          detail: { task_key: 'SECRET_EVENT_PAYLOAD' },
        }],
      },
    })

    expect(wrapper.get('.member-work-name').text()).toBe('读取 Agent')
    expect(wrapper.get('.member-work-role').text()).toBe('执行')
    expect(wrapper.get('.member-work-status-text').text()).toContain('读取项目源码')
    expect(wrapper.get('.member-work-task-title').text()).toBe('读取项目源码')
    expect(wrapper.get('.member-work-progress').text()).toBe('完成 0/2')
    expect(wrapper.find('.member-work-pulse').exists()).toBe(true)
    expect(wrapper.find('.member-work-timing').exists()).toBe(false)
    expect(wrapper.find('.member-work-log').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('SECRET_EVENT_PAYLOAD')
    expect(wrapper.find('.member-work-ask').exists()).toBe(false)
  })

  it('没有 running 任务时保持简洁的成员状态和完成计数', () => {
    const wrapper = mount(AgentMemberWorkCard, {
      props: {
        member: { ...baseMember, status: 'running', started_at: null },
        tasks: [{ ...tasks[0], status: 'completed' }],
      },
    })

    expect(wrapper.get('.member-work-status-text').text()).toBe('思考中…')
    expect(wrapper.get('.member-work-progress').text()).toBe('完成 1/1')
    expect(wrapper.text()).not.toContain('已完成 1 项任务')
  })

  it('showAsk 时渲染追问按钮并发出 ask 事件', async () => {
    const wrapper = mount(AgentMemberWorkCard, {
      props: { member: baseMember, tasks, showAsk: true },
    })
    const button = wrapper.get('.member-work-ask')
    await button.trigger('click')
    expect(wrapper.emitted('ask')?.[0]?.[0]).toMatchObject({ member_id: 1 })
  })

  it('展示任务标题、状态和进度，隐藏派发指令并保留单任务停止操作', async () => {
    const assignedTasks: AgentTeamTask[] = [
      { ...tasks[0], instructions: 'SECRET_TASK_INSTRUCTIONS' },
      tasks[1],
    ]
    const wrapper = mount(AgentMemberWorkCard, {
      props: { member: baseMember, tasks: assignedTasks, showStop: true },
    })

    expect(wrapper.findAll('.member-work-task')).toHaveLength(2)
    expect(wrapper.get('.member-work-progress').text()).toBe('完成 0/2')
    expect(wrapper.text()).toContain('读取项目源码')
    expect(wrapper.text()).not.toContain('SECRET_TASK_INSTRUCTIONS')
    expect(wrapper.find('.member-work-task-brief').exists()).toBe(false)
    await wrapper.get('.member-work-stop').trigger('click')
    expect(wrapper.emitted('stop')?.[0]?.[0]).toMatchObject({ task_key: 'read' })
  })

  it('未开启 showStop 时不显示停止按钮', () => {
    const wrapper = mount(AgentMemberWorkCard, { props: { member: baseMember, tasks } })
    expect(wrapper.find('.member-work-stop').exists()).toBe(false)
  })
})
