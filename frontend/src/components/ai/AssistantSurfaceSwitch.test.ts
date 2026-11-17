import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import AssistantSurfaceSwitch from './AssistantSurfaceSwitch.vue'

describe('AssistantSurfaceSwitch', () => {
  it('仅在切换时派发目标 surface，并显示管理未读', async () => {
    const wrapper = mount(AssistantSurfaceSwitch, { props: { current: 'user', adminUnread: 1 } })
    const user = wrapper.get('[data-surface="user"]')
    const admin = wrapper.get('[data-surface="admin"]')
    expect(user.attributes('aria-pressed')).toBe('true')
    expect(admin.attributes('aria-pressed')).toBe('false')
    expect(admin.find('[aria-label="有未读异常"]').exists()).toBe(true)
    await user.trigger('click')
    expect(wrapper.emitted('select')).toBeUndefined()
    await admin.trigger('click')
    expect(wrapper.emitted('select')).toEqual([['admin']])
  })
})
