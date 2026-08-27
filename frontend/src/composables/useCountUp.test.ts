import { defineComponent, nextTick, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useCountUp } from './useCountUp'

describe('useCountUp', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('lands immediately when reduced motion is requested', async () => {
    vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: true } as MediaQueryList)
    const source = ref(0)
    const Component = defineComponent({
      setup() {
        return { display: useCountUp(source) }
      },
      template: '<span>{{ display }}</span>',
    })
    const wrapper = mount(Component)

    source.value = 88
    await nextTick()
    expect(wrapper.text()).toBe('88')
  })

  it('cancels an outstanding animation frame when its component unmounts', async () => {
    vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: false } as MediaQueryList)
    vi.spyOn(window, 'requestAnimationFrame').mockReturnValue(41)
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
    wrapper.unmount()
    expect(cancel).toHaveBeenCalledWith(41)
  })
})
