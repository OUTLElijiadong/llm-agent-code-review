import { describe, expect, it } from 'vitest'

import { actionableError, readableError } from './withFeedback'

describe('actionableError', () => {
  it('读取拦截器直接抛出的业务错误元数据', () => {
    const result = actionableError({
      code: 50301,
      message: '模型服务暂时不可用',
      request_id: 'trace-1',
      retryable: true,
      next_action: '请稍后重试',
    })

    expect(result).toEqual({
      code: 50301,
      message: '模型服务暂时不可用',
      requestId: 'trace-1',
      retryable: true,
      nextAction: '请稍后重试',
    })
    expect(readableError(result)).toBe('模型服务暂时不可用')
  })

  it('读取 Axios 响应并用响应头补全请求编号', () => {
    const result = actionableError({
      response: {
        data: { code: 40300, message: '无权执行', retryable: false },
        headers: { 'x-request-id': 'trace-header' },
      },
    })

    expect(result).toMatchObject({
      code: 40300,
      message: '无权执行',
      requestId: 'trace-header',
      retryable: false,
    })
  })

  it('网络错误允许人工重试，未知对象保持保守不可重试', () => {
    expect(actionableError(new Error('Network Error'))).toMatchObject({
      message: 'Network Error', retryable: true,
    })
    expect(actionableError({}, '操作失败')).toMatchObject({
      message: '操作失败', retryable: false,
    })
  })

  it('已收到但缺少重试契约的响应默认不可安全重放', () => {
    expect(actionableError({
      response: { data: { message: '请求可能已处理' } },
    })).toMatchObject({
      message: '请求可能已处理', retryable: false,
    })
  })
})
