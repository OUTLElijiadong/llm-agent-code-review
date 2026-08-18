import { afterEach, vi } from 'vitest'

/**
 * jsdom 缺失的浏览器 API 桩:
 * AiOrb / FluidProgress 等动效组件依赖 IntersectionObserver、ResizeObserver、
 * matchMedia 与 canvas 上下文,测试环境统一降级为「不可见、无动画」,
 * 组件按降级路径静默渲染即可。
 */
if (typeof window !== 'undefined') {
  if (!window.IntersectionObserver) {
    class IntersectionObserverStub {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    }
    Object.defineProperty(window, 'IntersectionObserver', {
      writable: true,
      value: IntersectionObserverStub,
    })
  }

  if (!window.ResizeObserver) {
    class ResizeObserverStub {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    }
    Object.defineProperty(window, 'ResizeObserver', {
      writable: true,
      value: ResizeObserverStub,
    })
  }

  if (!window.matchMedia) {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: (query: string) => ({
        matches: false,
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        onchange: null,
        dispatchEvent: vi.fn(() => false),
      }),
    })
  }

  // jsdom 的 canvas 没有渲染上下文,getContext 返回 null,
  // FluidProgress 会自然走 WebGL/2D 双降级路径;这里补上避免抛异常。
  if (typeof HTMLCanvasElement !== 'undefined') {
    HTMLCanvasElement.prototype.getContext = vi.fn().mockReturnValue(null) as never
  }
}

/**
 * 清理每个前端单元测试产生的浏览器状态与计时器。
 * @returns 无返回值，仅恢复隔离测试环境。
 */
function resetBrowserTestState(): void {
  localStorage.clear()
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
}

afterEach(resetBrowserTestState)
