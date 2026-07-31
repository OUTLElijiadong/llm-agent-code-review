import type { Ref } from 'vue'
import { beforeEach, expect, it, vi } from 'vitest'

interface LifecycleState {
  mounted: (() => void) | null
  beforeUnmount: (() => void) | null
}

/**
 * 创建供生命周期模拟器共享的回调容器。
 * @returns 可记录挂载与卸载回调的状态对象。
 */
const lifecycle = vi.hoisted(function createLifecycleState(): LifecycleState {
  return { mounted: null, beforeUnmount: null }
})

/**
 * 用可控回调替代 Vue 生命周期注册，保留其余 Vue 导出。
 * @param importOriginal - Vitest 提供的原始 Vue 模块加载器。
 * @returns 覆盖生命周期函数后的 Vue 模块。
 */
vi.mock('vue', async function mockVueLifecycle(importOriginal) {
  const actual = await importOriginal<typeof import('vue')>()
  return {
    ...actual,
    onMounted: vi.fn(
      /**
       * 保存组件挂载阶段应执行的回调。
       * @param callback - useTilt 注册的挂载回调。
       * @returns 无返回值，仅记录回调。
       */
      function captureMounted(callback: () => void): void {
        lifecycle.mounted = callback
      },
    ),
    onBeforeUnmount: vi.fn(
      /**
       * 保存组件卸载前应执行的回调。
       * @param callback - useTilt 注册的卸载回调。
       * @returns 无返回值，仅记录回调。
       */
      function captureBeforeUnmount(callback: () => void): void {
        lifecycle.beforeUnmount = callback
      },
    ),
  }
})

import { useTilt } from '@/utils/tilt'

/**
 * 清理每个用例捕获的生命周期回调。
 * @returns 无返回值，仅重置隔离状态。
 */
beforeEach(function resetLifecycleState(): void {
  lifecycle.mounted = null
  lifecycle.beforeUnmount = null
})

/** 验证自定义参数下的倾斜计算、回弹样式与监听器清理。 */
it('applies custom tilt transforms and removes listeners on unmount', function testCustomTilt(): void {
  const element = document.createElement('article')
  const elementRef = { value: element } as Ref<HTMLElement | null>
  const rect = {
    left: 10,
    top: 20,
    width: 200,
    height: 100,
    right: 210,
    bottom: 120,
    x: 10,
    y: 20,
  } as DOMRect
  vi.spyOn(element, 'getBoundingClientRect').mockReturnValue(rect)
  const removeListener = vi.spyOn(element, 'removeEventListener')

  useTilt(elementRef, { max: 10, perspective: 1000, scale: 1.1, speed: 250 })
  expect(lifecycle.mounted).toBeTypeOf('function')
  expect(lifecycle.beforeUnmount).toBeTypeOf('function')
  lifecycle.mounted!()

  element.dispatchEvent(new MouseEvent('mousemove', { clientX: 160, clientY: 45 }))
  expect(element.style.transition).toBe(
    'transform 0.08s ease-out, box-shadow 0.08s ease-out',
  )
  expect(element.style.transform).toBe(
    'perspective(1000px) rotateX(5.00deg) rotateY(5.00deg) scale3d(1.1, 1.1, 1.1)',
  )

  element.dispatchEvent(new MouseEvent('mouseleave'))
  expect(element.style.transition).toBe(
    'transform 250ms cubic-bezier(0.25, 1, 0.5, 1), box-shadow 250ms cubic-bezier(0.25, 1, 0.5, 1)',
  )
  expect(element.style.transform).toBe(
    'perspective(1000px) rotateX(0deg) rotateY(0deg) scale3d(1, 1, 1)',
  )

  lifecycle.beforeUnmount!()
  expect(removeListener).toHaveBeenCalledWith('mousemove', expect.any(Function))
  expect(removeListener).toHaveBeenCalledWith('mouseleave', expect.any(Function))
  element.style.transform = 'translateZ(0)'
  element.dispatchEvent(new MouseEvent('mousemove', { clientX: 160, clientY: 45 }))
  expect(element.style.transform).toBe('translateZ(0)')
})

/** 验证缺省参数会生成预期的最大角度、透视、缩放和回弹速度。 */
it('uses the documented default tilt options', function testDefaultTilt(): void {
  const element = document.createElement('section')
  const elementRef = { value: element } as Ref<HTMLElement | null>
  const rect = {
    left: 0,
    top: 0,
    width: 100,
    height: 50,
    right: 100,
    bottom: 50,
    x: 0,
    y: 0,
  } as DOMRect
  vi.spyOn(element, 'getBoundingClientRect').mockReturnValue(rect)

  useTilt(elementRef)
  lifecycle.mounted!()
  element.dispatchEvent(new MouseEvent('mousemove', { clientX: 0, clientY: 0 }))
  expect(element.style.transform).toBe(
    'perspective(800px) rotateX(8.00deg) rotateY(-8.00deg) scale3d(1.015, 1.015, 1.015)',
  )

  element.dispatchEvent(new MouseEvent('mouseleave'))
  expect(element.style.transition).toContain('transform 400ms')
  expect(element.style.transform).toBe(
    'perspective(800px) rotateX(0deg) rotateY(0deg) scale3d(1, 1, 1)',
  )
})

/** 验证元素尚未绑定时挂载与卸载流程保持安全无副作用。 */
it('handles a null element reference safely', function testNullElementRef(): void {
  const elementRef = { value: null } as Ref<HTMLElement | null>

  useTilt(elementRef)
  expect(function mountWithoutElement(): void {
    lifecycle.mounted!()
  }).not.toThrow()
  expect(function unmountWithoutElement(): void {
    lifecycle.beforeUnmount!()
  }).not.toThrow()
})
