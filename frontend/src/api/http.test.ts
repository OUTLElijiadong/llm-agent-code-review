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
  delete window.__prismAuthExpiredHandled
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

  it('keeps background transport failures local while still reporting server failures', async () => {
    harness.messageError.mockClear()
    const config = { silentTransportError: true }
    const disconnected = { config, code: 'ERR_NETWORK', message: 'Network Error' }
    await expect(harness.state.responseRejected!(disconnected)).rejects.toBe(disconnected)
    expect(harness.messageError).not.toHaveBeenCalled()

    const forbidden = { code: 40300, message: '无权访问', data: null }
    await expect(harness.state.responseRejected!({
      config,
      response: { status: 403, data: forbidden },
      message: 'Forbidden',
    })).rejects.toBe(forbidden)
    expect(harness.messageError).toHaveBeenCalledWith('无权访问')
  })

  it('解析 Blob 形式的领域导出错误并保留下一步操作', async () => {
    const data = { code: 40941, message: '领域报告不支持 PDF', next_action: '请导出真实领域 JSON', retryable: false }
    const blob = new Blob([JSON.stringify(data)], { type: 'application/json' })
    await expect(harness.state.responseRejected!({
      response: { status: 409, data: blob }, message: 'Request failed', config: { responseType: 'blob' },
    })).rejects.toMatchObject(data)
    expect(harness.messageError).toHaveBeenCalledWith('领域报告不支持 PDF')
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

describe('登录冷却响应', () => {
  it('登录错密由表单显示，不触发会话过期、跳转或重复 toast', async () => {
    const data = { code: 40001, message: '用户名或密码错误', data: null }
    await expect(harness.state.responseRejected!({
      config: { url: '/auth/login' }, response: { status: 401, data },
    })).rejects.toBe(data)
    expect(harness.clearToken).not.toHaveBeenCalled()
    expect(harness.routerReplace).not.toHaveBeenCalled()
    expect(harness.messageError).not.toHaveBeenCalled()
  })

  it('应用 429 保留 Retry-After 剩余秒并由登录表单唯一提示', async () => {
    await expect(harness.state.responseRejected!({
      config: { url: '/auth/login' },
      response: { status: 429, data: { code: 42900, message: '稍后再试' }, headers: { 'retry-after': '37' } },
    })).rejects.toMatchObject({ code: 42900, message: '稍后再试', retry_after_seconds: 37 })
    expect(harness.messageError).not.toHaveBeenCalled()
    expect(harness.routerReplace).not.toHaveBeenCalled()
  })

  it('代理 HTML 429 解析 HTTP-date，使用服务器 Date 避免本机时钟偏差', async () => {
    await expect(harness.state.responseRejected!({
      config: { url: '/auth/login' },
      response: { status: 429, data: '<html>Too Many Requests</html>', headers: {
        'retry-after': 'Mon, 07 Sep 2026 08:01:00 GMT', date: 'Mon, 07 Sep 2026 08:00:15 GMT',
      } },
    })).rejects.toMatchObject({ code: 42900, retry_after_seconds: 45, message: '登录请求过于频繁，请等待后重试' })
    expect(harness.messageError).not.toHaveBeenCalled()
  })

  it.each(['invalid', '-3', '1.5', ''])('无效 Retry-After %s 不捏造倒计时，允许读取应用剩余秒', async (value) => {
    await expect(harness.state.responseRejected!({
      config: { url: '/auth/login' },
      response: { status: 429, data: { code: 42900, message: '限流', retry_after_seconds: 12 }, headers: { 'retry-after': value } },
    })).rejects.toMatchObject({ retry_after_seconds: 12 })
  })

  it('代理无剩余值时只说明稍后重试，不编造 60 秒', async () => {
    const failure = await harness.state.responseRejected!({
      config: { url: '/auth/login' }, response: { status: 429, data: '<html>limited</html>' },
    }).catch((error) => error)
    expect(failure.message).toBe('登录请求过于频繁，请等待后重试')
    expect(failure.retry_after_seconds).toBeUndefined()
  })
})


it('代理 503 的 Retry-After 也交给登录页，不假装密码错误', async () => {
  await expect(harness.state.responseRejected!({
    config: { url: '/auth/login' },
    response: { status: 503, data: '<html>unavailable</html>', headers: { 'Retry-After': '90' } },
  })).rejects.toMatchObject({ code: 50301, retry_after_seconds: 90 })
  expect(harness.messageError).not.toHaveBeenCalled()
})

it('Retry-After 的零秒与已过去 HTTP-date 都不会被改成默认冷却', async () => {
  for (const headers of [
    { 'retry-after': '0' },
    { 'retry-after': 'Mon, 07 Sep 2026 08:00:00 GMT', date: 'Mon, 07 Sep 2026 08:00:15 GMT' },
  ]) {
    await expect(harness.state.responseRejected!({
      config: { url: '/auth/login' }, response: { status: 429, data: {}, headers },
    })).rejects.toMatchObject({ retry_after_seconds: 0 })
  }
})

describe('退出或换账号后的迟到401', () => {
  it.each([null, 'new-account-token'])('请求携带旧Token而当前为%s时，不清理或打断当前会话', async (currentToken) => {
    harness.getToken.mockReturnValue('old-token')
    const config = harness.state.requestFulfilled!({ url: '/agent-mesh/inbox', headers: {} })
    harness.getToken.mockReturnValue(currentToken)
    const dispatch = vi.spyOn(window, 'dispatchEvent')
    const data = { code: 40100, message: '缺少token', data: null }
    await expect(harness.state.responseRejected!({ config, response: { status: 401, data } })).rejects.toBe(data)
    expect(harness.clearToken).not.toHaveBeenCalled()
    expect(dispatch).not.toHaveBeenCalled()
    expect(harness.messageError).not.toHaveBeenCalled()
    expect(harness.routerReplace).not.toHaveBeenCalled()
    expect(window.__prismAuthExpiredHandled).not.toBe(true)
  })

  it('旧账号迟到40102不踢掉新账号，新账号自己的过期401仍提示', async () => {
    harness.getToken.mockReturnValue('new-token')
    const oldError = { config: { url: '/agent-mesh/inbox', headers: { authorization: 'Bearer old-token' } },
      response: { status: 401, data: { code: 40102, message: '已下线', data: null } } }
    await expect(harness.state.responseRejected!(oldError)).rejects.toBe(oldError.response.data)
    expect(harness.messageError).not.toHaveBeenCalled()
    const currentError = { config: { url: '/projects', headers: { get: () => 'Bearer new-token' } },
      response: { status: 401, data: { code: 40100, message: '当前登录已过期', data: null } } }
    await expect(harness.state.responseRejected!(currentError)).rejects.toBe(currentError.response.data)
    expect(harness.messageError).toHaveBeenCalledWith('当前登录已过期')
    expect(harness.clearToken).toHaveBeenCalledOnce()
    expect(harness.routerReplace).toHaveBeenCalledWith('/login')
  })

  it('真正匿名访问受限资源的401仍提示登录', async () => {
    harness.getToken.mockReturnValue(null)
    const data = { code: 40100, message: '缺少token', data: null }
    await expect(harness.state.responseRejected!({ config: { url: '/projects', headers: {} }, response: { status: 401, data } })).rejects.toBe(data)
    expect(harness.messageError).toHaveBeenCalledWith('缺少token')
    expect(harness.routerReplace).toHaveBeenCalledWith('/login')
  })

  it('当前账号的40102仍提供单设备下线提示', async () => {
    harness.getToken.mockReturnValue('same-token')
    const data = { code: 40102, message: '已下线', data: null }
    await expect(harness.state.responseRejected!({ config: { url: '/projects', headers: { Authorization: 'Bearer same-token' } }, response: { status: 401, data } })).rejects.toBe(data)
    expect(harness.messageError).toHaveBeenCalledWith('账号已在另一台设备登录，当前设备已下线')
    expect(harness.clearToken).toHaveBeenCalledOnce()
  })
})
