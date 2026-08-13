import { ElMessage } from 'element-plus/es/components/message/index'

/**
 * 全站统一的操作错误反馈兜底。
 *
 * 治理目标:大量 `catch {}` 静默吞掉错误,用户操作失败毫无感知。
 * 本模块提供「静默捕获 → 统一给出人话提示」的兜底,避免每个页面各写一套。
 *
 * 注意:项目的 http 拦截器可能已对部分错误弹过提示;这里的兜底文案
 * 应尽量只在确实没有反馈时补一条,避免重复打扰。
 */

/** 从异常对象提取可读信息(优先后端 message,再 Error.message,最后兜底)。 */
export function readableError(error: unknown, fallback = '操作失败,请重试'): string {
  if (error && typeof error === 'object') {
    const anyErr = error as Record<string, unknown>
    const respMsg = (anyErr.response as Record<string, unknown> | undefined)?.data
    if (respMsg && typeof respMsg === 'object') {
      const msg = (respMsg as Record<string, unknown>).message
      if (typeof msg === 'string' && msg.trim()) return msg
    }
    if (typeof anyErr.message === 'string' && anyErr.message.trim()) return anyErr.message
  }
  if (typeof error === 'string' && error.trim()) return error
  return fallback
}

/**
 * 包装一个异步操作,失败时统一弹出人话错误提示(不再静默)。
 * 用于替换 `try { ... } catch {}` 的静默写法。
 *
 * @param fn 异步操作
 * @param errFallback 失败时的兜底文案(无可读信息时用)
 * @returns 操作结果;失败时返回 undefined 并已提示
 */
export async function withFeedback<T>(fn: () => Promise<T> | T, errFallback?: string): Promise<T | undefined> {
  try {
    return await fn()
  } catch (error) {
    // 用户主动取消(ElMessageBox 取消会 reject 'cancel')不算错误,不提示
    if (error === 'cancel' || (error && typeof error === 'object' && (error as Error).name === 'AbortError')) {
      return undefined
    }
    ElMessage.error(readableError(error, errFallback))
    return undefined
  }
}
