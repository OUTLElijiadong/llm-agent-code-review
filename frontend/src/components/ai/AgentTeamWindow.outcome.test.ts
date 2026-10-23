import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { describe, expect, it } from 'vitest'

import type { AgentTeamDetail } from '@/api/agentTeams'
import AgentTeamWindow from './AgentTeamWindow.vue'

describe('AgentTeamWindow 团队状态展示', () => {
  it('阻塞团队只展示状态与任务，不展示结论、原因和执行明细', () => {
    const team: AgentTeamDetail = {
      team_id: 91,
      title: '阻塞团队',
      surface: 'user',
      session_id: 'session',
      status: 'blocked',
      max_active_children: 2,
      trace_id: 'trace',
      summary: { final_result: { summary: 'SECRET_FINAL_RESULT' } },
      error: { reason: 'SECRET_TEAM_REASON', failed_tasks: ['read'] },
      members: [],
      tasks: [{
        task_id: 1,
        task_key: 'read',
        member_id: 1,
        title: '读取论坛',
        instructions: 'SECRET_INSTRUCTIONS',
        depends_on: [],
        status: 'blocked',
        priority: 1,
        attempt_count: 1,
        max_attempts: 3,
        result: { summary: 'SECRET_RESULT' },
        errors: [{ message: 'SECRET_ERROR' }],
      }],
      counts: { total: 1, completed: 0, running: 0, queued: 0, failed: 0, blocked: 1 },
      events: [],
      messages: [],
    }

    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team },
      global: { stubs: { Teleport: true, Transition: false, 'el-icon': true }, plugins: [createPinia()] },
    })

    expect(wrapper.get('.team-window-status').text()).toBe('已阻塞')
    expect(wrapper.get('.team-window-progress').text()).toContain('已完成 0/1 项')
    expect(wrapper.get('.team-window-task-details').text()).toContain('读取论坛')
    expect(wrapper.get('.team-window-task-details').text()).toContain('已阻塞')
    expect(wrapper.find('.team-window-outcome').exists()).toBe(false)
    for (const secret of ['SECRET_FINAL_RESULT', 'SECRET_TEAM_REASON', 'SECRET_INSTRUCTIONS', 'SECRET_RESULT', 'SECRET_ERROR']) {
      expect(wrapper.text()).not.toContain(secret)
    }
    wrapper.unmount()
  })
})
