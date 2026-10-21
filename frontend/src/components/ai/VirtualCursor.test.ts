import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { requestXiaolingNavigation } from '@/utils/xiaolingNavigation'
import VirtualCursor from './VirtualCursor.vue'
import { useAgentActivityStore } from '@/stores/agentActivity'

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
    window.history.replaceState({}, '', '/')
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
    target.addEventListener('click', () => {
      navigate()
      window.history.pushState({}, '', '/reports')
    })
    document.body.appendChild(target)
    wrapper = mount(VirtualCursor, { global: { stubs: { Transition: false } } })

    requestXiaolingNavigation('/reports', '审查报告', vi.fn())
    await flushPromises()
    vi.advanceTimersByTime(1360)
    await flushPromises()

    expect(navigate).toHaveBeenCalledOnce()
    expect(wrapper.find('.virtual-cursor-click-label').text()).toBe('已打开')
    expect(wrapper.find('.virtual-cursor').exists()).toBe(true)
  })

  it('does not guess an unrelated create button when the tool has no authorized page target', async () => {
    const main = document.createElement('main')
    const unrelated = document.createElement('button')
    unrelated.textContent = '创建项目'
    unrelated.getBoundingClientRect = () => ({
      x: 20, y: 30, left: 20, top: 30, right: 140, bottom: 70,
      width: 120, height: 40, toJSON: () => ({}),
    })
    main.appendChild(unrelated)
    document.body.appendChild(main)
    wrapper = mount(VirtualCursor, { global: { stubs: { Transition: false } } })
    useAgentActivityStore().begin('小菱正在回复反馈', 'unknown-target')
    await flushPromises()
    vi.advanceTimersByTime(1400)
    await flushPromises()
    expect(unrelated.classList.contains('xl-vcursor-target')).toBe(false)
    expect(wrapper.find('.virtual-cursor').exists()).toBe(false)
    main.remove()
  })

  it('does not point to or activate a route hidden behind a modal', async () => {
    target = document.createElement('button')
    target.dataset.route = '/reports'
    target.getBoundingClientRect = () => ({ left: 20, top: 30, right: 140, bottom: 70, width: 120, height: 40 } as DOMRect)
    const clicked = vi.fn()
    target.addEventListener('click', clicked)
    document.body.appendChild(target)
    const modal = document.createElement('div')
    document.body.appendChild(modal)
    const original = document.elementFromPoint
    document.elementFromPoint = vi.fn(() => modal)
    try {
      wrapper = mount(VirtualCursor, { global: { stubs: { Transition: false } } })
      requestXiaolingNavigation('/reports', '审查报告', vi.fn())
      await flushPromises()
      vi.advanceTimersByTime(1400)
      await flushPromises()
      expect(clicked).not.toHaveBeenCalled()
      expect(wrapper.find('.virtual-cursor-layer').exists()).toBe(false)
    } finally {
      document.elementFromPoint = original
      modal.remove()
    }
  })

  it('still executes the authorized navigation when the route button is covered', async () => {
    target = document.createElement('button')
    target.dataset.route = '/reports'
    target.getBoundingClientRect = () => ({ left: 20, top: 30, right: 140, bottom: 70, width: 120, height: 40 } as DOMRect)
    document.body.appendChild(target)
    const modal = document.createElement('div')
    document.body.appendChild(modal)
    const original = document.elementFromPoint
    document.elementFromPoint = vi.fn(() => modal)
    const execute = vi.fn()
    try {
      wrapper = mount(VirtualCursor, { global: { stubs: { Transition: false } } })
      requestXiaolingNavigation('/reports', '审查报告', execute)
      await flushPromises()
      vi.advanceTimersByTime(1400)
      await flushPromises()
      expect(execute).toHaveBeenCalledOnce()
    } finally {
      document.elementFromPoint = original
      modal.remove()
    }
  })

  it('treats a redirected legacy route as a successful navigation', async () => {
    wrapper = mount(VirtualCursor, { global: { stubs: { Transition: false } } })
    const execute = vi.fn(() => {
      window.history.pushState({}, '', '/admin/governance?section=agents')
    })
    requestXiaolingNavigation('/admin/agents', 'Agent 管理', execute)
    await flushPromises()
    vi.advanceTimersByTime(1400)
    await flushPromises()
    vi.advanceTimersByTime(100)
    await flushPromises()
    expect(execute).toHaveBeenCalledOnce()
    expect(wrapper.find('.virtual-cursor-click-label').text()).toBe('已打开')
  })

  it('executes a pending navigation once when the activity is cleared before the click', async () => {
    wrapper = mount(VirtualCursor, { global: { stubs: { Transition: false } } })
    const execute = vi.fn()
    requestXiaolingNavigation('/reviews/7', '审查详情', execute)
    await flushPromises()
    useAgentActivityStore().clear()
    await flushPromises()
    vi.advanceTimersByTime(5000)
    await flushPromises()
    expect(execute).toHaveBeenCalledOnce()
  })

  it('visualizes the exact browser bridge element without clicking it again', async () => {
    target = document.createElement('button')
    target.getBoundingClientRect = () => ({ left: 20, top: 30, right: 140, bottom: 70, width: 120, height: 40 } as DOMRect)
    const clicked = vi.fn()
    target.addEventListener('click', clicked)
    document.body.appendChild(target)
    wrapper = mount(VirtualCursor, { global: { stubs: { Transition: false } } })
    window.dispatchEvent(new CustomEvent('prism:site-browser-target', { detail: { element: target, action: 'click', label: '论坛发表' } }))
    await flushPromises()
    vi.advanceTimersByTime(800)
    await flushPromises()
    expect(target.classList.contains('xl-vcursor-target')).toBe(true)
    expect(wrapper.find('.virtual-cursor').exists()).toBe(true)
    expect(clicked).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1000)
    await flushPromises()
    expect(wrapper.find('.virtual-cursor').exists()).toBe(false)
  })

  it('uses the authorized callback once when no page navigation element exists', async () => {
    const navigate = vi.fn()
    wrapper = mount(VirtualCursor, { global: { stubs: { Transition: false } } })

    requestXiaolingNavigation('/reviews/42', '审查详情', () => {
      navigate()
      window.history.pushState({}, '', '/reviews/42')
    })
    await flushPromises()
    vi.advanceTimersByTime(1400)
    await flushPromises()

    expect(navigate).toHaveBeenCalledOnce()
  })

  it('skips a hidden matching route button and clicks the visible one', async () => {
    const hidden = document.createElement('button')
    hidden.dataset.route = '/reports'
    hidden.style.display = 'none'
    const hiddenClick = vi.fn()
    hidden.addEventListener('click', hiddenClick)
    document.body.appendChild(hidden)

    const visibleClick = vi.fn()
    target = document.createElement('button')
    target.dataset.route = '/reports'
    target.getBoundingClientRect = () => ({
      x: 30, y: 40, left: 30, top: 40, right: 150, bottom: 80,
      width: 120, height: 40, toJSON: () => ({}),
    })
    target.addEventListener('click', () => {
      visibleClick()
      window.history.pushState({}, '', '/reports')
    })
    document.body.appendChild(target)
    wrapper = mount(VirtualCursor, { global: { stubs: { Transition: false } } })

    requestXiaolingNavigation('/reports', '审查报告', vi.fn())
    await flushPromises()
    vi.advanceTimersByTime(1400)
    await flushPromises()

    expect(hiddenClick).not.toHaveBeenCalled()
    expect(visibleClick).toHaveBeenCalledOnce()
    hidden.remove()
  })

  it('shows a failure state when the authorized navigation callback rejects', async () => {
    wrapper = mount(VirtualCursor, { global: { stubs: { Transition: false } } })

    requestXiaolingNavigation('/reviews/42', '审查详情', async () => {
      throw new Error('navigation failed')
    })
    await flushPromises()
    vi.advanceTimersByTime(1400)
    await flushPromises()

    expect(wrapper.find('.virtual-cursor-click-label').text()).toBe('跳转失败')
  })

  it('scrolls an offscreen route button into view before the visible click', async () => {
    const navigate = vi.fn()
    let scrolled = false
    target = document.createElement('button')
    target.dataset.route = '/admin/audit'
    target.textContent = '系统操作审计'
    target.scrollIntoView = vi.fn(() => {
      scrolled = true
    })
    target.getBoundingClientRect = () => ({
      x: 20,
      y: scrolled ? 120 : window.innerHeight + 80,
      left: 20,
      top: scrolled ? 120 : window.innerHeight + 80,
      right: 180,
      bottom: scrolled ? 160 : window.innerHeight + 120,
      width: 160,
      height: 40,
      toJSON: () => ({}),
    })
    target.addEventListener('click', navigate)
    document.body.appendChild(target)
    wrapper = mount(VirtualCursor, { global: { stubs: { Transition: false } } })

    requestXiaolingNavigation('/admin/audit', '系统操作审计', vi.fn())
    await flushPromises()
    vi.advanceTimersByTime(1400)
    await flushPromises()

    expect(target.scrollIntoView).toHaveBeenCalledWith({
      behavior: 'auto',
      block: 'center',
      inline: 'nearest',
    })
    expect(navigate).toHaveBeenCalledOnce()
  })

  it('uses the clicked Agent link for same-page query navigation instead of an unrelated primary action', async () => {
    window.history.replaceState({}, '', '/reviews?tab=all')
    const navigate = vi.fn()
    const main = document.createElement('main')
    const unrelated = document.createElement('button')
    unrelated.textContent = '创建任务'
    main.appendChild(unrelated)
    document.body.appendChild(main)
    target = document.createElement('button')
    target.textContent = '我的审查'
    target.getBoundingClientRect = () => ({
      x: 30, y: 40, left: 30, top: 40, right: 150, bottom: 80,
      width: 120, height: 40, toJSON: () => ({}),
    })
    document.body.appendChild(target)
    wrapper = mount(VirtualCursor, { global: { stubs: { Transition: false } } })

    requestXiaolingNavigation('/reviews?tab=mine#latest', '我的审查', navigate, target)
    await flushPromises()
    vi.advanceTimersByTime(800)
    await flushPromises()

    expect(target.classList.contains('xl-vcursor-target')).toBe(true)
    expect(unrelated.classList.contains('xl-vcursor-target')).toBe(false)
    vi.advanceTimersByTime(700)
    await flushPromises()
    expect(navigate).toHaveBeenCalledOnce()
    main.remove()
  })
})
