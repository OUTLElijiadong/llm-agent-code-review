import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import type { AgentTeamDetail, AgentTeamMessage } from '@/api/agentTeams'
import AgentTeamWindow from './AgentTeamWindow.vue'

const global = { stubs: { Teleport: true, Transition: false, 'el-icon': true } }

function msg(id: string, sentFrom: string): AgentTeamMessage {
  return {
    message_id: id, trace_id: 't', correlation_id: 'c', sent_from: sentFrom, send_to: 'session:user:s',
    message_type: 'task.result', subject: `来自 ${sentFrom}`, status: 'completed', create_time: '2026-10-01T12:00:00Z',
  }
}

function blockedTeam(): AgentTeamDetail {
  return {
    team_id: 91, title: '阻塞团队', surface: 'user', session_id: 's', status: 'blocked',
    max_active_children: 2, trace_id: 'trace', created_at: '2026-10-01T12:00:00Z',
    error: { failed_tasks: ['read'] },
    members: [{
      member_id: 1, member_key: 'reader', display_name: '读取成员', address: 'temporary:reader', role: 'worker',
      status: 'failed',
    } as never],
    tasks: [{
      task_id: 1, task_key: 'read', member_id: 1, member_key: 'reader', title: '读取论坛', depends_on: [],
      instructions: '只读取论坛最近帖子', status: 'blocked', priority: 1, attempt_count: 1, max_attempts: 3,
      result: { status: 'blocked', summary: '缺少论坛查询执行合同' },
      errors: [{ code: 'no_execution_contract', message: '缺少论坛查询执行合同' }],
    }],
    counts: { total: 1, completed: 0, running: 0, queued: 0, failed: 0, blocked: 1 },
    events: [],
    messages: [msg('m-user', 'user:7'), msg('m-orch', 'agent:orchestrator'), msg('m-sys', 'system:agent_team'),
      msg('m-main', 'session:user:s')],
  }
}

describe('AgentTeamWindow 终态说明与发言者', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('阻塞团队显示原因、任务指令与结果，并允许从阻塞根任务重试', () => {
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: blockedTeam() }, global: { ...global, plugins: [createPinia()] },
    })
    const outcome = wrapper.get('.team-window-outcome')
    expect(outcome.text()).toContain('团队被阻塞')
    expect(outcome.text()).toContain('读取论坛：缺少论坛查询执行合同')
    expect(wrapper.get('.team-window-status').text()).toBe('已阻塞')
    expect(wrapper.get('.team-window-task-instructions').text()).toContain('只读取论坛最近帖子')
    expect(wrapper.get('.team-window-task-result').text()).toContain('结果：缺少论坛查询执行合同')
  })

  it('用户、系统与小菱各角色不再被统一记成小菱', () => {
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: blockedTeam() }, global: { ...global, plugins: [createPinia()] },
    })
    const heads = wrapper.findAll('.team-chat-head').map((row) => row.text())
    expect(heads[0]).toContain('你')
    expect(heads[0]).toContain('用户')
    expect(heads[1]).toContain('小菱')
    expect(heads[1]).toContain('编排')
    expect(heads[2]).toContain('系统')
    expect(heads[3]).toContain('小菱')
    expect(heads[3]).toContain('主持')
  })
})
