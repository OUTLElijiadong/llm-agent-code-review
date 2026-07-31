import { afterEach, vi } from 'vitest'

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
