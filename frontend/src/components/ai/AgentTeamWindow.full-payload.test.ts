import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { describe, expect, it } from 'vitest'

import type { AgentTeamDetail, AgentTeamMember, AgentTeamMessage, AgentTeamTask } from '@/api/agentTeams'
import AgentTeamWindow from './AgentTeamWindow.vue'

const global = { stubs: { Teleport: true, Transition: false, 'el-icon': true } }

describe('AgentTeamWindow 精简团队视图', () => {
  it('只展示团队、成员、任务、状态和进度，不渲染结论或协作正文', () => {
    const member: AgentTeamMember = {
      member_id: 1,
      member_key: 'reviewer',
      display_name: '代码审查员',
      address: 'agent:reviewer',
      kind: 'runtime',
      role: 'worker',
      status: 'running',
    }
    const task: AgentTeamTask = {
      task_id: 7,
      task_key: 'audit-error',
      member_id: 1,
      member_key: member.member_key,
      title: '检查鉴权边界',
      instructions: 'SECRET_TASK_INSTRUCTIONS',
      depends_on: [],
      status: 'running',
      priority: 1,
      attempt_count: 1,
      max_attempts: 2,
      result: { summary: 'SECRET_TASK_RESULT', verification: 'SECRET_VERIFICATION' },
      errors: [{ message: 'SECRET_TASK_ERROR' }],
    }
    const message: AgentTeamMessage = {
      message_id: 'secret-message',
      trace_id: 'trace',
      correlation_id: 'correlation',
      sent_from: 'agent:reviewer',
      send_to: 'manager',
      message_type: 'task.result',
      subject: 'SECRET_MESSAGE_SUBJECT',
      status: 'completed',
      payload: { report: 'SECRET_MESSAGE_PAYLOAD' },
    }
    const team: AgentTeamDetail = {
      team_id: 82,
      title: '并行审查团队',
      objective: 'SECRET_TEAM_OBJECTIVE',
      surface: 'user',
      session_id: 'team-session',
      status: 'running',
      max_active_children: 2,
      trace_id: 'trace',
      summary: { final_result: { summary: 'SECRET_TEAM_FINAL_RESULT' } },
      error: { reason: 'SECRET_TEAM_ERROR' },
      members: [member],
      tasks: [task],
      counts: { total: 1, completed: 0, running: 1, queued: 0, failed: 0, blocked: 0 },
      events: [],
      messages: [message],
    }

    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team },
      global: { ...global, plugins: [createPinia()] },
    })

    expect(wrapper.get('.team-window-title').text()).toContain('并行审查团队')
    expect(wrapper.get('.team-window-status').text()).toBe('运行中')
    expect(wrapper.get('.team-window-progress').text()).toContain('已完成 0/1 项')
    expect(wrapper.get('.member-work-name').text()).toBe('代码审查员')
    expect(wrapper.get('.member-work-task-title').text()).toBe('检查鉴权边界')
    expect(wrapper.get('.member-work-task-status').text()).toBe('进行中')
    expect(wrapper.get('.team-window-task-details').text()).toContain('代码审查员')

    for (const secret of [
      'SECRET_TEAM_OBJECTIVE', 'SECRET_TEAM_FINAL_RESULT', 'SECRET_TEAM_ERROR',
      'SECRET_TASK_INSTRUCTIONS', 'SECRET_TASK_RESULT', 'SECRET_VERIFICATION',
      'SECRET_TASK_ERROR', 'SECRET_MESSAGE_SUBJECT', 'SECRET_MESSAGE_PAYLOAD',
    ]) {
      expect(wrapper.text()).not.toContain(secret)
    }
    expect(wrapper.find('.team-window-outcome').exists()).toBe(false)
    expect(wrapper.find('.team-window-chat').exists()).toBe(false)
    wrapper.unmount()
  })
})
