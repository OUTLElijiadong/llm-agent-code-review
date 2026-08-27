import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { AgentTeamDetail } from '@/api/agentTeams'
import AgentTeamCard from './AgentTeamCard.vue'

function makeTeam(): AgentTeamDetail {
  return {
    team_id: 7,
    title: '代码审查团队',
    objective: '检查项目风险',
    surface: 'user',
    session_id: 'session-7',
    status: 'running',
    created_at: '2026-08-27T00:00:00Z',
    started_at: '2026-08-27T00:00:00Z',
    completed_at: null,
    max_active_children: 2,
    trace_id: 'trace-7',
    counts: { total: 1, completed: 0, running: 1, queued: 0, failed: 0, blocked: 0 },
    members: [{
      member_id: 1,
      member_key: 'scanner',
      display_name: '扫描员',
      address: 'agent:scanner',
      kind: 'runtime',
      role: 'worker',
      status: 'running',
    }],
    tasks: [],
    events: [],
    messages: [],
  }
}

function mountCard() {
  return mount(AgentTeamCard, {
    props: { team: makeTeam() },
    global: {
      stubs: {
        'el-icon': { template: '<span><slot /></span>' },
        Loading: true,
        CircleCheck: true,
        WarningFilled: true,
        Timer: true,
      },
    },
  })
}

describe('AgentTeamCard keyboard interaction', () => {
  it('团队卡片支持 Enter 和空格打开详情', async () => {
    const wrapper = mountCard()
    const card = wrapper.get('.team-card')

    expect(card.attributes('role')).toBe('button')
    expect(card.attributes('tabindex')).toBe('0')
    await card.trigger('keydown.enter')
    await card.trigger('keydown.space')

    expect(wrapper.emitted('open-panel')).toEqual([[7], [7]])
    wrapper.unmount()
  })

  it('成员徽章支持键盘打开详情且不重复触发卡片', async () => {
    const wrapper = mountCard()
    const badge = wrapper.get('.team-member-badge')

    expect(badge.attributes('role')).toBe('button')
    expect(badge.attributes('tabindex')).toBe('0')
    await badge.trigger('keydown.enter')

    expect(wrapper.emitted('open-panel')).toEqual([[7]])
    wrapper.unmount()
  })
})
