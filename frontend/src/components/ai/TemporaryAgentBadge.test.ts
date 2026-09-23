import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import AgentMemberWorkCard from './AgentMemberWorkCard.vue'
import AgentTeamMemberBadge from './AgentTeamMemberBadge.vue'

it.each(['temporary', 'runtime'])('成员工作卡仅为 %s 临时成员标记当前任务边界', (kind) => {
  const wrapper = mount(AgentMemberWorkCard, { props: { member: {
    member_id: 1, member_key: 'focused-review', display_name: '专项审查', address: 'temporary:focused-review',
    kind, role: 'worker', status: 'completed', capabilities: { dispatch_state: 'team_only' },
  } } })
  expect(wrapper.text().includes('本任务临时')).toBe(kind === 'temporary')
  wrapper.unmount()
})

describe('紧凑成员徽标', () => {
  it('临时属性可见且可被辅助技术读取', () => {
    const wrapper = mount(AgentTeamMemberBadge, { props: { name: '专项审查', status: 'running', kind: 'temporary' } })
    expect(wrapper.text()).toContain('本任务临时')
    expect(wrapper.attributes('aria-label')).toContain('本任务临时')
    wrapper.unmount()
  })
})
