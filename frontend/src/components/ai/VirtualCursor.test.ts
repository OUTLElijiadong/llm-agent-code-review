import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { requestXiaolingNavigation } from '@/utils/xiaolingNavigation'
import VirtualCursor from './VirtualCursor.vue'

describe('VirtualCursor real navigation click', () => {
  let wrapper: ReturnType<typeof mount> | null = null
  let target: HTMLButtonElement | null = null

  beforeEach(() => {
    vi.useFakeTimers()
    setActivePinia(createPinia())
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      callback(0)
      return 1
    })
    vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => undefined)
  })

  afterEach(() => {
    wrapper?.unmount()
    target?.remove()
    wrapper = null
    target = null
  })

  it('moves to a visible route button and triggers its real click', async () => {
    const navigate = vi.fn()
    target = document.createElement('button')
    target.dataset.route = '/reports'
    target.textContent = '审查报告'
    target.getBoundingClientRect = () => ({
      x: 20, y: 30, left: 20, top: 30, right: 140, bottom: 70,
      width: 120, height: 40, toJSON: () => ({}),
    })
    target.addEventListener('click', navigate)
    document.body.appendChild(target)
    wrapper = mount(VirtualCursor, { global: { stubs: { Transition: false } } })

    requestXiaolingNavigation('/reports', '审查报告', vi.fn())
    await flushPromises()
    vi.advanceTimersByTime(1400)
    await flushPromises()

    expect(navigate).toHaveBeenCalledOnce()
    expect(wrapper.find('.virtual-cursor').exists()).toBe(true)
  })

  it('uses the authorized callback once when no page navigation element exists', async () => {
    const navigate = vi.fn()
    wrapper = mount(VirtualCursor, { global: { stubs: { Transition: false } } })

    requestXiaolingNavigation('/reviews/42', '审查详情', navigate)
    await flushPromises()
    vi.advanceTimersByTime(1400)
    await flushPromises()

    expect(navigate).toHaveBeenCalledOnce()
  })
})
