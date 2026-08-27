import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  XIAOLING_NAVIGATION_EVENT,
  isSafeXiaolingRoute,
  requestXiaolingNavigation,
  type XiaolingNavigationRequest,
} from './xiaolingNavigation'

describe('xiaoling visual navigation request', () => {
  const listeners: EventListener[] = []

  afterEach(() => {
    for (const listener of listeners) {
      window.removeEventListener(XIAOLING_NAVIGATION_EVENT, listener)
    }
    listeners.splice(0)
  })

  it('rejects external targets before executing', () => {
    const execute = vi.fn()

    expect(isSafeXiaolingRoute('/reviews?tab=mine')).toBe(true)
    expect(requestXiaolingNavigation('//evil.example/reviews', '错误目标', execute)).toBe(false)
    expect(requestXiaolingNavigation('https://evil.example/reviews', '错误目标', execute)).toBe(false)
    expect(execute).not.toHaveBeenCalled()
  })

  it('falls back to direct navigation when the global cursor is absent', () => {
    const execute = vi.fn()

    expect(requestXiaolingNavigation('/reviews', '审查记录', execute)).toBe(true)
    expect(execute).toHaveBeenCalledOnce()
  })

  it('lets the global cursor take ownership without navigating twice', () => {
    const execute = vi.fn()
    const listener: EventListener = (event) => {
      const detail = (event as CustomEvent<XiaolingNavigationRequest>).detail
      detail.handled = true
    }
    listeners.push(listener)
    window.addEventListener(XIAOLING_NAVIGATION_EVENT, listener)

    expect(requestXiaolingNavigation('/reports', '审查报告', execute)).toBe(true)
    expect(execute).not.toHaveBeenCalled()
  })
})
