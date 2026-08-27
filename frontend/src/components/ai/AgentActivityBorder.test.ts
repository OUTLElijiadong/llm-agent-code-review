import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { useAgentActivityStore } from '@/stores/agentActivity'
import AgentActivityBorder from './AgentActivityBorder.vue'

describe('AgentActivityBorder accessibility', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('keeps decorative edges hidden while exposing the live work status', () => {
    const store = useAgentActivityStore()
    store.begin('小菱正在打开项目管理…', 'nav-test')
    const wrapper = mount(AgentActivityBorder, {
      global: { stubs: { Transition: false, PrismMascot: true } },
    })

    expect(wrapper.get('.agent-activity-border').attributes('aria-hidden')).toBeUndefined()
    expect(wrapper.get('[role="status"]').text()).toContain('小菱正在打开项目管理')
    expect(wrapper.get('.agent-activity-edge').attributes('aria-hidden')).toBe('true')
  })
})
