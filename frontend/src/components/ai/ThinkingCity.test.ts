import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { describe, expect, it, vi } from 'vitest'

const teams = vi.hoisted(() => ({ detail: vi.fn(), events: vi.fn() }))
vi.mock('@/api/agentTeams', () => ({ getAgentTeam: teams.detail, listAgentTeamEvents: teams.events }))
import ThinkingCity from './ThinkingCity.vue'

describe('ThinkingCity 聊天静态模式', () => {
  it('静态模式继续读取真实团队，开始、暂停、恢复均不启动动画帧', async () => {
    const request = vi.spyOn(window, 'requestAnimationFrame').mockReturnValue(1)
    teams.detail.mockResolvedValue({
      team_id: 42, title: '项目查询', status: 'completed', members: [], tasks: [],
    })
    const wrapper = mount(ThinkingCity, {
      props: { animated: false, active: true, teamIds: [42] },
      global: { plugins: [createPinia()] },
    })
    try {
      await flushPromises()
      expect(teams.detail).toHaveBeenCalledWith(42)
      expect(wrapper.find('canvas').exists()).toBe(true)
      await wrapper.setProps({ active: false })
      await wrapper.setProps({ active: true })
      expect(request).not.toHaveBeenCalled()
    } finally { wrapper.unmount() }
  })

  it('未指定静态模式的其他调用方保留原动画行为', () => {
    const request = vi.spyOn(window, 'requestAnimationFrame').mockReturnValue(2)
    const cancel = vi.spyOn(window, 'cancelAnimationFrame')
    const wrapper = mount(ThinkingCity, { global: { plugins: [createPinia()] } })
    expect(request).toHaveBeenCalledOnce()
    wrapper.unmount()
    expect(cancel).toHaveBeenCalledWith(2)
  })
})
