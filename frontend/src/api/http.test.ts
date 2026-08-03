import { beforeEach, describe, expect, it, vi } from 'vitest'
const harness = vi.hoisted(() => {
  const createConfigs: Record<string, any>[] = []
  const state: {
    requestFulfilled?: (config: Record<string, any>) => Record<string, any>
    responseFulfilled?: (response: Record<string, any>) => any
    responseRejected?: (error: Record<string, any>) => Promise<never>
  } = {}
  const instance = {
    interceptors: {
      request: {
        use: vi.fn((fulfilled: (config: Record<string, any>) => Record<string, any>) => {
          state.requestFulfilled = fulfilled
        }),
      },
      response: {
        use: vi.fn(
          (
            fulfilled: (response: Record<string, any>) => any,
            rejected: (error: Record<string, any>) => Promise<never>,
          ) => {
            state.responseFulfilled = fulfilled
            state.responseRejected = rejected
          },
        ),
      },
    },
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  }
  return {
    createConfigs,
    state,
    instance,
    create: vi.fn((config: Record<string, any>) => {
      createConfigs.push(config)
      return instance
    }),
    isCancel: vi.fn(() => false),
    messageError: vi.fn(),
    routerPush: vi.fn(),
    routerReplace: vi.fn(),
    getToken: vi.fn<() => string | null>(() => null),
    clearToken: vi.fn(),
  }
})

vi.mock('axios', () => ({
  default: {
    create: harness.create,
    isCancel: harness.isCancel,
  },
}))

vi.mock('element-plus/es/components/message/index', () => ({
  ElMessage: { error: harness.messageError },
}))

vi.mock('@/router', () => ({
  default: { push: harness.routerPush, replace: harness.routerReplace },
}))

vi.mock('@/utils/token', () => ({
  getToken: harness.getToken,
  clearToken: harness.clearToken,
}))

import http, {
  del as apiDelete,
  download,
  get as apiGet,
  post as apiPost,
  put as apiPut,
} from './http'

/** 重置 Axios 测试桩的返回行为。 */
function resetHttpHarness(): void {
  harness.getToken.mockReturnValue(null)
  harness.isCancel.mockReturnValue(false)
  harness.instance.get.mockReset()
  harness.instance.post.mockReset()
  harness.instance.put.mockReset()
  harness.instance.delete.mockReset()
}

beforeEach(resetHttpHarness)

describe('http interceptors', () => {
  it('creates the shared client with the production timeout', () => {
    /** 验证客户端基础配置与默认导出。 */
    expect(harness.createConfigs[0]).toEqual({ baseURL: '/api', timeout: 600_000 })
    expect(http).toBe(harness.instance)
  })

  it('injects a bearer token only when one exists', () => {
    /** 验证请求拦截器的 token 注入与空 token 分支。 */
    harness.getToken.mockReturnValue('token-1')
    const withToken = harness.state.requestFulfilled!({ headers: {} })
    expect(withToken.headers.Authorization).toBe('Bearer token-1')

    harness.getToken.mockReturnValue(null)
    const withoutToken = harness.state.requestFulfilled!({ headers: {} })
    expect(withoutToken.headers.Authorization).toBeUndefined()
  })

  it('accepts successful envelopes and blob downloads', () => {
    /** 验证业务成功与二进制响应均原样通过。 */
    const normal = { config: {}, data: { code: 0, message: 'ok', data: { id: 1 } } }
    const blob = { config: { responseType: 'blob' }, data: new Blob(['report']) }

    expect(harness.state.responseFulfilled!(normal)).toBe(normal)
    expect(harness.state.responseFulfilled!(blob)).toBe(blob)
    expect(harness.messageError).not.toHaveBeenCalled()
  })

  it('rejects business failures with the backend message', async () => {
    /** 验证 HTTP 200 但业务码失败时的提示与拒绝值。 */
    const data = { code: 40001, message: '参数错误', data: null }

    await expect(harness.state.responseFulfilled!({ config: {}, data })).rejects.toBe(data)
    expect(harness.messageError).toHaveBeenCalledWith('参数错误')
  })

  it('handles 401 by clearing auth state, emitting an event and redirecting', async () => {
    /** 验证登录过期的完整客户端联动。 */
    const dispatch = vi.spyOn(window, 'dispatchEvent')
    const data = { code: 40100, message: '登录已过期', data: null }
    const error = { response: { status: 401, data }, message: 'Unauthorized' }

    await expect(harness.state.responseRejected!(error)).rejects.toBe(data)
    expect(harness.clearToken).toHaveBeenCalledOnce()
    expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({ type: 'prism:auth-expired' }))
    expect(harness.routerReplace).toHaveBeenCalledWith('/login')
    expect(harness.messageError).toHaveBeenCalledWith('登录已过期')
  })

  it('uses the forced-offline message for a superseded single-device session', async () => {
    const data = { code: 40102, message: 'old message', data: null }
    const error = { response: { status: 401, data }, message: 'Unauthorized' }

    await expect(harness.state.responseRejected!(error)).rejects.toBe(data)
    expect(harness.messageError).toHaveBeenCalledWith('账号已在另一台设备登录，当前设备已下线')
  })

  it('reports 403 and network failures without clearing the token', async () => {
    /** 验证非 401 服务端错误与无响应网络错误映射。 */
    const forbidden = { code: 40300, message: '无权访问', data: null }
    await expect(
      harness.state.responseRejected!({ response: { status: 403, data: forbidden }, message: 'Forbidden' }),
    ).rejects.toBe(forbidden)
    expect(harness.messageError).toHaveBeenLastCalledWith('无权访问')
    expect(harness.clearToken).not.toHaveBeenCalled()

    const network = { message: 'Network Error' }
    await expect(harness.state.responseRejected!(network)).rejects.toBe(network)
    expect(harness.messageError).toHaveBeenLastCalledWith('Network Error')
  })

  it('silently propagates an explicitly cancelled request', async () => {
    /** 验证主动取消不弹错误、不跳登录，只把取消对象交还调用方。 */
    const cancelled = { message: 'canceled' }
    harness.isCancel.mockReturnValue(true)

    await expect(harness.state.responseRejected!(cancelled)).rejects.toBe(cancelled)
    expect(harness.messageError).not.toHaveBeenCalled()
    expect(harness.clearToken).not.toHaveBeenCalled()
    expect(harness.routerPush).not.toHaveBeenCalled()
  })
})

describe('http convenience functions', () => {
  it('cleans query params and unwraps get/post/put/delete envelopes', async () => {
    /** 验证四类 JSON helper 的参数清洗和 data 解包。 */
    harness.instance.get.mockResolvedValue({ data: { data: ['g'] } })
    harness.instance.post.mockResolvedValue({ data: { data: { id: 2 } } })
    harness.instance.put.mockResolvedValue({ data: { data: 'updated' } })
    harness.instance.delete.mockResolvedValue({ data: { data: true } })

    await expect(apiGet('/items', { page: 1, q: '', empty: null, missing: undefined })).resolves.toEqual(['g'])
    await expect(apiPost('/items', { name: 'n' }, { limit: 5, q: '' })).resolves.toEqual({ id: 2 })
    await expect(apiPut('/items/2', { name: 'next' })).resolves.toBe('updated')
    await expect(apiDelete('/items/2')).resolves.toBe(true)

    expect(harness.instance.get).toHaveBeenCalledWith('/items', { params: { page: 1 } })
    expect(harness.instance.post).toHaveBeenCalledWith('/items', { name: 'n' }, { params: { limit: 5 } })
    expect(harness.instance.put).toHaveBeenCalledWith('/items/2', { name: 'next' })
    expect(harness.instance.delete).toHaveBeenCalledWith('/items/2')
  })

  it('preserves omitted params and returns downloaded blobs', async () => {
    /** 验证无查询参数分支与 download 二进制契约。 */
    const blob = new Blob(['pdf'], { type: 'application/pdf' })
    harness.instance.get
      .mockResolvedValueOnce({ data: { data: 3 } })
      .mockResolvedValueOnce({ data: blob })

    await expect(apiGet('/count')).resolves.toBe(3)
    await expect(download('/report', { task_id: 1, q: '' })).resolves.toBe(blob)

    expect(harness.instance.get).toHaveBeenNthCalledWith(1, '/count', { params: undefined })
    expect(harness.instance.get).toHaveBeenNthCalledWith(2, '/report', {
      params: { task_id: 1 },
      responseType: 'blob',
    })
  })
})
