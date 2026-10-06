import { defineComponent, nextTick, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useCountUp } from './useCountUp'

describe('useCountUp', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows the first asynchronously loaded value immediately', async () => {
    vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: false } as MediaQueryList)
    const source = ref(0)
    const Component = defineComponent({
      setup() {
        return { display: useCountUp(source) }
      },
      template: '<span>{{ display }}</span>',
    })
    const wrapper = mount(Component)

    source.value = 64
    await nextTick()

    expect(wrapper.text()).toBe('64')
    wrapper.unmount()
  })

  it('lands immediately when reduced motion is requested', async () => {
    vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: true } as MediaQueryList)
    const request = vi.spyOn(window, 'requestAnimationFrame').mockReturnValue(41)
    const source = ref(0)
    const Component = defineComponent({
      setup() {
        return { display: useCountUp(source) }
      },
      template: '<span>{{ display }}</span>',
    })
    const wrapper = mount(Component)

    source.value = 10
    await nextTick()
    source.value = 88
    await nextTick()
    expect(wrapper.text()).toBe('88')
    expect(request).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('animates later updates along the cubic-out curve and lands on the exact target', async () => {
    vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: false } as MediaQueryList)
    vi.spyOn(performance, 'now').mockReturnValue(100)
    let pendingFrame: FrameRequestCallback | undefined
    let frameId = 0
    const request = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      pendingFrame = callback
      frameId += 1
      return frameId
    })
    const source = ref(0)
    const Component = defineComponent({
      setup() {
        return { display: useCountUp(source) }
      },
      template: '<span>{{ display }}</span>',
    })
    const wrapper = mount(Component)

    source.value = 10
    await nextTick()
    source.value = 20
    await nextTick()

    expect(wrapper.text()).toBe('10')
    expect(request).toHaveBeenCalledTimes(1)
    pendingFrame?.(425)
    await nextTick()
    expect(wrapper.text()).toBe('18.75')
    expect(request).toHaveBeenCalledTimes(2)

    pendingFrame?.(750)
    await nextTick()
    expect(wrapper.text()).toBe('20')
    expect(request).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('animates later value changes and cancels the frame when its component unmounts', async () => {
    vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: false } as MediaQueryList)
    const request = vi.spyOn(window, 'requestAnimationFrame').mockReturnValue(41)
    const cancel = vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => undefined)
    const source = ref(0)
    const Component = defineComponent({
      setup() {
        return { display: useCountUp(source) }
      },
      template: '<span>{{ display }}</span>',
    })
    const wrapper = mount(Component)

    source.value = 10
    await nextTick()
    expect(wrapper.text()).toBe('10')
    expect(request).not.toHaveBeenCalled()

    source.value = 20
    await nextTick()
    expect(request).toHaveBeenCalled()
    wrapper.unmount()
    expect(cancel).toHaveBeenCalledWith(41)
  })
})
