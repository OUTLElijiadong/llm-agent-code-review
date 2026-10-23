import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'

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

describe('AgentMemberWorkCard 展示边界回归', () => {
  it('异常、结果、验证、执行指令与事件详情不进入成员卡片', () => {
    const task: AgentTeamTask = {
      task_id: 1,
      task_key: 'review',
      member_id: 1,
      member_key: 'reader',
      title: '检查认证流程',
      instructions: 'SECRET_INSTRUCTIONS',
      depends_on: [],
      status: 'running',
      priority: 0,
      attempt_count: 1,
      max_attempts: 3,
      result: { verdict: 'SECRET_VERDICT', summary: 'SECRET_RESULT' },
      errors: [{ message: 'SECRET_ERROR' }],
    }
    const wrapper = mount(AgentMemberWorkCard, {
      props: {
        member: baseMember,
        tasks: [task],
        events: [{
          event_id: 1,
          team_id: 1,
          member_id: 1,
          event_type: 'task.claimed',
          detail: { command: 'SECRET_EVENT_DETAIL' },
        }],
      },
    })

    expect(wrapper.text()).toContain('检查认证流程')
    for (const secret of ['SECRET_INSTRUCTIONS', 'SECRET_VERDICT', 'SECRET_RESULT', 'SECRET_ERROR', 'SECRET_EVENT_DETAIL']) {
      expect(wrapper.text()).not.toContain(secret)
    }
    expect(wrapper.find('.member-work-log').exists()).toBe(false)
    expect(wrapper.find('.member-work-task-brief').exists()).toBe(false)
  })

  it('状态文案保持中文', () => {
    const running = mount(AgentMemberWorkCard, {
      props: { member: baseMember },
    })
    expect(running.text()).toContain('思考中…')
    expect(running.text()).not.toContain('started working')
    expect(running.text()).not.toContain('working for')
    expect(running.text()).not.toContain('queued')

    const queued = mount(AgentMemberWorkCard, {
      props: { member: { ...baseMember, status: 'created' } },
    })
    expect(queued.text()).toContain('排队等待派活')
  })
})

describe('历史bug回归:团队悬浮窗遮挡', () => {
  it('【修复】团队悬浮窗在聊天浮窗上方，且低于确认窗', async () => {
    const source = (await import('./AgentTeamWindow.vue?raw')).default as string
    const match = source.match(/\.agent-team-window\s*\{[^}]*z-index:\s*([^;]+);/s)
    expect(match).not.toBeNull()
    const declared = match![1].trim()
    const vars = readFileSync('src/assets/styles/variables.scss', 'utf8')
    const tokenValue = (token: string) => Number(vars.match(new RegExp(`${token}:\\s*(\\d+)`))?.[1])
    const token = declared.match(/var\((--[\w-]+)/)?.[1]
    expect(token).toBe('--layer-agent-team')
    expect(tokenValue(token!)).toBeGreaterThan(tokenValue('--layer-assistant'))
    expect(tokenValue(token!)).toBeLessThan(tokenValue('--layer-modal'))
  })
})
