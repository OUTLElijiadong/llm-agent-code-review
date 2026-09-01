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

export interface ActionableError {
  message: string
  code?: number
  requestId?: string
  retryable: boolean
  nextAction?: string
}

/** 把 Axios、业务信封或原生 Error 归一为面向用户的恢复信息。 */
export function actionableError(error: unknown, fallback = '操作失败,请重试'): ActionableError {
  const root = error && typeof error === 'object' ? error as Record<string, unknown> : undefined
  const response = root?.response && typeof root.response === 'object'
    ? root.response as Record<string, unknown>
    : undefined
  const payload = response?.data && typeof response.data === 'object'
    ? response.data as Record<string, unknown>
    : root
  const headers = response?.headers && typeof response.headers === 'object'
    ? response.headers as Record<string, unknown>
    : undefined
  const payloadMessage = payload?.message
  const rootMessage = root?.message
  const message = typeof payloadMessage === 'string' && payloadMessage.trim()
    ? payloadMessage
    : typeof rootMessage === 'string' && rootMessage.trim()
      ? rootMessage
      : typeof error === 'string' && error.trim()
        ? error
        : fallback
  const code = typeof payload?.code === 'number' ? payload.code : undefined
  const bodyRequestId = payload?.request_id
  const headerRequestId = headers?.['x-request-id'] ?? headers?.['X-Request-Id']
  const requestId = typeof bodyRequestId === 'string' && bodyRequestId.trim()
    ? bodyRequestId
    : typeof headerRequestId === 'string' && headerRequestId.trim()
      ? headerRequestId
      : undefined
  // 收到响应但缺少明确契约时，不能把可能已落地的副作用当成可安全重放。
  const retryable = typeof payload?.retryable === 'boolean'
    ? payload.retryable
    : !response && error instanceof Error
  const nextAction = typeof payload?.next_action === 'string' && payload.next_action.trim()
    ? payload.next_action
    : undefined
  return { message, code, requestId, retryable, nextAction }
}

/** 从异常对象提取可读信息(优先后端 message,再 Error.message,最后兜底)。 */
export function readableError(error: unknown, fallback = '操作失败,请重试'): string {
  return actionableError(error, fallback).message
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
