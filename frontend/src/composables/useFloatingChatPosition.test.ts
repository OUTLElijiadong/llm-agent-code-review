import { defineComponent, nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useFloatingChatPosition } from './useFloatingChatPosition'

function setViewport(width: number, height: number): void {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: height })
}

describe('useFloatingChatPosition', () => {
  beforeEach(() => {
    setViewport(1280, 720)
  })

  it('drops a saved desktop position on mobile and restores it when desktop returns', async () => {
    localStorage.setItem('prism-floating-chat-position:user', JSON.stringify({ left: 856, top: 76 }))
    const Component = defineComponent({
      setup() {
        return useFloatingChatPosition('user')
      },
      template: '<div ref="panelRef" :style="style" />',
    })
    const wrapper = mount(Component)
    Object.defineProperty(wrapper.element, 'offsetWidth', { configurable: true, value: 358 })
    Object.defineProperty(wrapper.element, 'offsetHeight', { configurable: true, value: 600 })

    wrapper.vm.restoreOrAnchor()
    await nextTick()
    expect(wrapper.attributes('style')).toContain('left: 856px')

    setViewport(390, 844)
    window.dispatchEvent(new Event('resize'))
    await nextTick()
    expect(wrapper.attributes('style') ?? '').not.toContain('left:')
    expect(wrapper.attributes('style') ?? '').not.toContain('top:')

    setViewport(1280, 720)
    window.dispatchEvent(new Event('resize'))
    await nextTick()
    expect(wrapper.attributes('style')).toContain('left: 856px')

    wrapper.unmount()
  })

  it('does not apply a desktop position when first opened on mobile', async () => {
    setViewport(390, 844)
    localStorage.setItem('prism-floating-chat-position:admin', JSON.stringify({ left: 820, top: 60 }))
    const Component = defineComponent({
      setup() {
        return useFloatingChatPosition('admin')
      },
      template: '<div ref="panelRef" :style="style" />',
    })
    const wrapper = mount(Component)
    Object.defineProperty(wrapper.element, 'offsetWidth', { configurable: true, value: 366 })
    Object.defineProperty(wrapper.element, 'offsetHeight', { configurable: true, value: 620 })

    wrapper.vm.restoreOrAnchor()
    await nextTick()
    expect(wrapper.attributes('style') ?? '').not.toContain('left:')
    expect(wrapper.attributes('style') ?? '').not.toContain('top:')

    wrapper.unmount()
  })

  it('removes its resize listener when the owner unmounts', () => {
    const remove = vi.spyOn(window, 'removeEventListener')
    const Component = defineComponent({
      setup() {
        return useFloatingChatPosition('cleanup')
      },
      template: '<div ref="panelRef" />',
    })
    const wrapper = mount(Component)

    wrapper.unmount()

    expect(remove).toHaveBeenCalledWith('resize', expect.any(Function))
  })
})
