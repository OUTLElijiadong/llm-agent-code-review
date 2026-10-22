import { defineComponent, nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useFloatingChatPosition } from './useFloatingChatPosition'

function setViewport(width: number, height: number): void {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: height })
}

describe('useFloatingChatPosition', () => {
  beforeEach(() => {
    setViewport(1280, 720)
  })

  afterEach(() => vi.restoreAllMocks())

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

  it('重登自动恢复时等待真实尺寸，不用 0×0 把浮窗放到视口外', async () => {
    setViewport(1440, 1000)
    let width = 0
    let height = 0
    let scheduled: FrameRequestCallback | undefined
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      scheduled = callback
      return 1
    })
    const Component = defineComponent({
      setup() {
        return useFloatingChatPosition('user:2')
      },
      template: '<div ref="panelRef" :style="style" />',
    })
    const wrapper = mount(Component)
    Object.defineProperty(wrapper.element, 'offsetWidth', { configurable: true, get: () => width })
    Object.defineProperty(wrapper.element, 'offsetHeight', { configurable: true, get: () => height })

    wrapper.vm.restoreOrAnchor()
    expect(scheduled).toBeTypeOf('function')
    expect(wrapper.attributes('style') ?? '').not.toContain('left:')

    width = 400
    height = 620
    scheduled?.(0)
    await nextTick()
    expect(wrapper.attributes('style')).toContain('left: 1016px')
    expect(wrapper.attributes('style')).toContain('top: 356px')
    wrapper.unmount()
  })

  it('鼠标缩放将窗口限制在最小和最大尺寸，并持久化', async () => {
    localStorage.setItem('prism-floating-chat-position:resize', JSON.stringify({
      left: 40, top: 40, width: 400, height: 620,
    }))
    const Component = defineComponent({
      setup() { return useFloatingChatPosition('resize') },
      template: '<div ref="panelRef" :style="style" />',
    })
    const wrapper = mount(Component)
    Object.defineProperty(wrapper.element, 'offsetWidth', { configurable: true, value: 400 })
    Object.defineProperty(wrapper.element, 'offsetHeight', { configurable: true, value: 620 })
    wrapper.vm.restoreOrAnchor()
    await nextTick()

    const handle = document.createElement('button')
    wrapper.vm.beginResize({
      button: 0, clientX: 100, clientY: 100, pointerId: 1,
      currentTarget: handle, preventDefault: vi.fn(),
    } as unknown as PointerEvent)
    wrapper.vm.moveDrag({ clientX: 900, clientY: 400 } as PointerEvent)
    await nextTick()
    expect(wrapper.attributes('style')).toContain('width: 760px')
    expect(wrapper.attributes('style')).toContain('height: 668px')

    wrapper.vm.moveDrag({ clientX: -1000, clientY: -1000 } as PointerEvent)
    await nextTick()
    expect(wrapper.attributes('style')).toContain('width: 360px')
    expect(wrapper.attributes('style')).toContain('height: 420px')
    wrapper.vm.endDrag()
    expect(localStorage.getItem('prism-floating-chat-position:resize')).toContain('"width":360')
    wrapper.unmount()
  })

  it('键盘缩放支持方向键并在视口变化后重新约束位置和尺寸', async () => {
    localStorage.setItem('prism-floating-chat-position:keyboard', JSON.stringify({
      left: 800, top: 50, width: 700, height: 620,
    }))
    const Component = defineComponent({
      setup() { return useFloatingChatPosition('keyboard') },
      template: '<div ref="panelRef" :style="style" />',
    })
    const wrapper = mount(Component)
    Object.defineProperty(wrapper.element, 'offsetWidth', { configurable: true, value: 400 })
    Object.defineProperty(wrapper.element, 'offsetHeight', { configurable: true, value: 620 })
    wrapper.vm.restoreOrAnchor()
    await nextTick()
    const down = { key: 'ArrowDown', preventDefault: vi.fn() } as unknown as KeyboardEvent
    wrapper.vm.resizeByKeyboard(down)
    await nextTick()
    expect(down.preventDefault).toHaveBeenCalledOnce()
    expect(wrapper.attributes('style')).toContain('height: 636px')

    setViewport(521, 560)
    window.dispatchEvent(new Event('resize'))
    await nextTick()
    expect(wrapper.attributes('style')).toContain('width: 497px')
    expect(wrapper.attributes('style')).toContain('height: 536px')
    setViewport(521, 480)
    window.dispatchEvent(new Event('resize'))
    await nextTick()
    expect(wrapper.attributes('style') ?? '').not.toContain('left:')
    expect(wrapper.attributes('style') ?? '').not.toContain('top:')
    expect(wrapper.attributes('style') ?? '').not.toContain('width:')
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


describe('浮窗边角与响应式约束', () => {
  function panel(key = 'boundary') {
    const Component = defineComponent({
      setup: () => useFloatingChatPosition(key),
      template: '<div ref="panelRef" :style="style" />',
    })
    const wrapper = mount(Component)
    Object.defineProperty(wrapper.element, 'offsetWidth', { configurable: true, get: () => Number.parseFloat((wrapper.element as HTMLElement).style.width) || 400 })
    Object.defineProperty(wrapper.element, 'offsetHeight', { configurable: true, get: () => Number.parseFloat((wrapper.element as HTMLElement).style.height) || 620 })
    wrapper.vm.restoreOrAnchor()
    return wrapper
  }
  function pointer(x: number, y: number, id = 7) {
    return { button: 0, clientX: x, clientY: y, pointerId: id,
      currentTarget: document.createElement('button'), preventDefault: vi.fn(),
    } as unknown as PointerEvent
  }
  beforeEach(() => {
    setViewport(1280, 900)
    localStorage.setItem('prism-floating-chat-position:boundary', JSON.stringify({ left: 200, top: 200, width: 400, height: 620 }))
  })
  it.each(['nw', 'n', 'w'] as const)('%s 边角放大时固定对边，保存位置与尺寸', async (direction) => {
    const wrapper = panel()
    try {
      wrapper.vm.beginResize(pointer(200, 200), direction)
      wrapper.vm.moveDrag(pointer(100, 100))
      wrapper.vm.endDrag()
      await nextTick()
      const state = JSON.parse(localStorage.getItem('prism-floating-chat-position:boundary')!)
      expect(state.width).toBe(direction.includes('w') ? 500 : 400)
      expect(state.height).toBe(direction.includes('n') ? 720 : 620)
      expect(state.left + state.width).toBe(600)
      expect(state.top + state.height).toBe(820)
    } finally { wrapper.unmount() }
  })
  it('右下角到达视口边界后不再推动窗口原点', async () => {
    const wrapper = panel()
    try {
      wrapper.vm.beginResize(pointer(600, 820), 'se')
      wrapper.vm.moveDrag(pointer(2000, 2000))
      await nextTick()
      expect(wrapper.attributes('style')).toContain('left: 200px')
      expect(wrapper.attributes('style')).toContain('top: 200px')
      expect(wrapper.attributes('style')).toContain('width: 760px')
      expect(wrapper.attributes('style')).toContain('height: 688px')
    } finally { wrapper.unmount() }
  })
  it('忽略其他指针，捕获丢失后停止调整，键盘仍可继续操作', async () => {
    const wrapper = panel()
    try {
      wrapper.vm.beginResize(pointer(600, 820))
      wrapper.vm.moveDrag(pointer(700, 850, 99))
      await nextTick()
      expect(wrapper.attributes('style')).toContain('width: 400px')
      wrapper.vm.endDrag()
      wrapper.vm.moveDrag(pointer(700, 850))
      expect(wrapper.vm.resizing).toBe(false)
      wrapper.vm.resizeByKeyboard({ key: 'ArrowRight', shiftKey: true, preventDefault: vi.fn() } as unknown as KeyboardEvent)
      await nextTick()
      expect(wrapper.attributes('style')).toContain('width: 464px')
    } finally { wrapper.unmount() }
  })
  it.each([[390, 844], [844, 390]])('%sx%s 由响应式布局接管，旋转回来恢复桌面尺寸', async (width, height) => {
    const wrapper = panel()
    try {
      wrapper.vm.beginResize(pointer(600, 820))
      setViewport(width, height)
      window.dispatchEvent(new Event('resize'))
      await nextTick()
      expect(wrapper.attributes('style') || '').not.toContain('width:')
      expect(wrapper.vm.resizing).toBe(false)
      wrapper.vm.beginResize(pointer(600, 820))
      expect(wrapper.vm.resizing).toBe(false)
      setViewport(1280, 900)
      window.dispatchEvent(new Event('resize'))
      await nextTick()
      expect(wrapper.attributes('style')).toContain('width: 400px')
    } finally { wrapper.unmount() }
  })
  it('卸载时取消等待布局的帧，不重复注册恢复回调', () => {
    const request = vi.spyOn(window, 'requestAnimationFrame').mockReturnValue(23)
    const cancel = vi.spyOn(window, 'cancelAnimationFrame')
    const Component = defineComponent({ setup: () => useFloatingChatPosition('pending'), template: '<div ref="panelRef" />' })
    const wrapper = mount(Component)
    wrapper.vm.restoreOrAnchor()
    wrapper.vm.restoreOrAnchor()
    expect(request).toHaveBeenCalledTimes(1)
    wrapper.unmount()
    expect(cancel).toHaveBeenCalledWith(23)
  })
})
