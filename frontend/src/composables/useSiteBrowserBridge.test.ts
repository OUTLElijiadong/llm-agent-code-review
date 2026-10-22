import { mount } from '@vue/test-utils'
import { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios'
import { defineComponent, h, reactive, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Router } from 'vue-router'
import http from '@/api/http'
import { siteBrowserApi, type SiteBrowserRequiredEvent } from '@/api/siteBrowser'
import { ElMessage } from 'element-plus/es/components/message/index'
import { useSiteBrowserBridge } from './useSiteBrowserBridge'

vi.mock('@/router', () => ({ default: { replace: vi.fn() } }))
vi.mock('@/utils/token', () => ({ getToken: () => actor.token, clearToken: vi.fn() }))
vi.mock('@/stores/user', () => ({ useUserStore: () => actor }))
vi.mock('@/stores/agentActivity', () => ({ useAgentActivityStore: () => ({ begin: vi.fn(), end: vi.fn() }) }))
vi.mock('@/utils/agentNavigation', () => ({ isNavigationPathAllowed: () => true }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { error: vi.fn() } }))

const actor = reactive({ profile: { id: 7 }, token: 'browser-owner-token' })
const sessionLookup = vi.hoisted(() => vi.fn())
vi.mock('@/api/agentResponses', () => ({ getAgentResponseSession: sessionLookup }))

const originalAdapter = http.defaults.adapter
const wrappers: Array<ReturnType<typeof mount>> = []

function response(config: InternalAxiosRequestConfig, status: number, data: unknown): AxiosResponse {
  return { config, status, statusText: String(status), headers: {}, data }
}

function rejectRequest(config: InternalAxiosRequestConfig, status: number, code: number, message: string): never {
  throw new AxiosError(String(status), 'ERR_BAD_REQUEST', config, undefined,
    response(config, status, { code, message, data: null }))
}

function mountBridge(surface: 'user' | 'admin') {
  const sessionId = ref('browser-session'), runId = ref(''), status = ref(''), stopped = ref(false)
  const currentRoute = ref({ fullPath: '/forum' })
  const navigate = vi.fn(async (path: string) => { currentRoute.value.fullPath = path })
  wrappers.push(mount(defineComponent({
    setup() {
      useSiteBrowserBridge({
        surface,
        sessionId: () => sessionId.value,
        runId: () => runId.value,
        status: () => status.value,
        isStopped: () => stopped.value,
        router: { currentRoute, push: navigate } as unknown as Router,
      })
      return () => h('main')
    },
  })))
  return { sessionId, runId, status, stopped, navigate }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  sessionStorage.clear()
  actor.profile.id = 7
  actor.token = 'browser-owner-token'
  sessionLookup.mockReset()
})
afterEach(() => {
  for (const wrapper of wrappers.splice(0)) wrapper.unmount()
  http.defaults.adapter = originalAdapter
  vi.useRealTimers()
})

describe('页面桥的运行创建与恢复时序', () => {
  it.each(['pending', 'claim'])('人工停止后迟到的%s不能触发导航或成功回执', async stage => {
    sessionLookup.mockResolvedValue({ surface: 'user', session_id: 'browser-session',
      run: { run_id: 'run-stop-in-flight', status: 'running' } })
    const action: SiteBrowserRequiredEvent = {
      type: 'response.browser.required', action_id: 91, run_id: 'run-stop-in-flight',
      session_id: 'browser-session', surface: 'user', account_id: 7, client_nonce: 'nonce',
      action: 'navigate', args: { route: '/forum/42' }, observation_snapshot_id: 'snapshot-91',
      expires_at: new Date(Date.now() + 60000).toISOString(),
    }
    let finish!: () => void
    const requests: string[] = []
    http.defaults.adapter = async config => {
      requests.push(config.url || '')
      const data = config.url?.endsWith('/pending') ? { actions: [action] } : { action_id: 91, claim_token: 'claim' }
      const reply = response(config, 200, { code: 0, data })
      if (config.url?.endsWith(`/${stage}`)) return new Promise(resolve => { finish = () => resolve(reply) })
      return reply
    }
    const scope = mountBridge('user')
    scope.status.value = 'running'
    scope.runId.value = action.run_id
    await vi.advanceTimersByTimeAsync(0)
    expect(finish).toBeTypeOf('function')
    scope.stopped.value = true
    finish()
    await vi.advanceTimersByTimeAsync(5000)
    expect(scope.navigate).not.toHaveBeenCalled()
    expect(requests.some(path => path.endsWith('/ack'))).toBe(false)
    expect(requests.filter(path => path.endsWith('/claim'))).toHaveLength(stage === 'claim' ? 1 : 0)
  })

  it('人工停止尚未收到服务端终态时，迟到确认仍不能恢复页面动作', async () => {
    let finish!: (value: unknown) => void
    sessionLookup.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const requests: string[] = []
    http.defaults.adapter = async config => {
      requests.push(config.url || '')
      return response(config, 200, { code: 0, data: { actions: [] } })
    }
    const scope = mountBridge('user')
    scope.status.value = 'running'
    scope.runId.value = 'run-stopped-locally'
    await vi.advanceTimersByTimeAsync(0)
    scope.stopped.value = true
    finish({ surface: 'user', session_id: 'browser-session', run: { run_id: 'run-stopped-locally', status: 'running' } })
    await vi.advanceTimersByTimeAsync(5000)
    expect(requests).toEqual([])
    expect(sessionLookup).toHaveBeenCalledTimes(1)
    expect(scope.navigate).not.toHaveBeenCalled()
  })

  it.each(['session', 'account'] as const)('当前%s尚未恢复时不向服务器确认运行', async missing => {
    const scope = mountBridge('user')
    if (missing === 'session') scope.sessionId.value = ''
    else actor.profile.id = 0
    scope.status.value = 'running'
    scope.runId.value = 'run-before-owner-ready'
    await vi.advanceTimersByTimeAsync(4100)
    expect(sessionLookup).not.toHaveBeenCalled()
  })

  it('确认请求期间进入审批后能继续恢复，不遗留恢复锁', async () => {
    let finish: (value: unknown) => void = () => undefined
    sessionLookup.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    sessionLookup.mockResolvedValue({
      surface: 'user', session_id: 'browser-session', run: { run_id: 'run-approval', status: 'running' },
    })
    const requests: string[] = []
    http.defaults.adapter = async config => {
      requests.push(config.url || '')
      return response(config, 200, { code: 0, message: 'ok', data: { actions: [] } })
    }
    const scope = mountBridge('user')
    scope.status.value = 'running'
    scope.runId.value = 'run-approval'
    await vi.advanceTimersByTimeAsync(0)
    scope.status.value = 'approving'
    finish({ surface: 'user', session_id: 'browser-session', run: { run_id: 'run-approval', status: 'running' } })
    await vi.advanceTimersByTimeAsync(0)
    expect(requests).toHaveLength(0)
    scope.status.value = 'running'
    await vi.advanceTimersByTimeAsync(0)
    expect(requests).toEqual(['/agent-browser/pending'])
  })

  it.each(['user', 'admin'] as const)('客户端先发布 %s run ID 时，未落库的恢复不弹错且落库后执行排队动作', async surface => {
    let committed = false
    sessionLookup.mockImplementation(async () => ({
      surface, session_id: 'browser-session',
      run: committed ? { run_id: 'run_client_before_checkpoint', status: 'running' } : null,
    }))
    const requests: InternalAxiosRequestConfig[] = []
    const action: SiteBrowserRequiredEvent = {
      type: 'response.browser.required',
      action_id: 9,
      run_id: 'run_client_before_checkpoint',
      session_id: 'browser-session',
      surface,
      account_id: 7,
      client_nonce: 'owner-nonce',
      action: 'navigate',
      args: { route: '/forum/42' },
      observation_snapshot_id: 'snapshot-9',
      expires_at: new Date(Date.now() + 60000).toISOString(),
    }
    http.defaults.adapter = async config => {
      requests.push(config)
      if (config.url?.endsWith('/pending')) {
        if (!committed) rejectRequest(config, 404, 40400, '页面操作运行不存在')
        return response(config, 200, { code: 0, message: 'ok', data: { actions: [action] } })
      }
      if (config.url?.endsWith('/claim')) {
        return response(config, 200, { code: 0, message: 'ok', data: { action_id: 9, claim_token: 'claim-9' } })
      }
      if (config.url?.endsWith('/ack')) return response(config, 200, { code: 0, message: 'ok', data: {} })
      throw new Error(`意外的页面桥请求: ${config.url}`)
    }
    const scope = mountBridge(surface)
    // 与两个聊天入口相同：先写入客户端生成的 run，再发启动请求。
    scope.status.value = 'running'
    scope.runId.value = action.run_id
    await vi.advanceTimersByTimeAsync(0)
    expect(sessionLookup).toHaveBeenCalledExactlyOnceWith(surface, 'browser-session', true)
    expect(requests).toHaveLength(0)
    expect(scope.navigate).not.toHaveBeenCalled()
    expect(ElMessage.error).not.toHaveBeenCalled()

    committed = true
    await vi.advanceTimersByTimeAsync(2080)
    expect(requests.map(request => request.url)).toEqual([
      '/agent-browser/pending',
      '/agent-browser/actions/9/claim', '/agent-browser/actions/9/ack',
    ])
    expect(requests[0]!.params).toEqual({ session_id: 'browser-session', surface, run_id: action.run_id })
    expect(JSON.parse(requests[1]!.data)).toEqual({ session_id: 'browser-session', surface, client_nonce: 'owner-nonce' })
    expect(scope.navigate).toHaveBeenCalledExactlyOnceWith('/forum/42')
    expect(JSON.parse(requests[2]!.data)).toMatchObject({
      session_id: 'browser-session', surface, client_nonce: 'owner-nonce', claim_token: 'claim-9', outcome: 'completed',
      observation: { snapshot_id: 'snapshot-9', route: '/forum/42' },
    })
    expect(ElMessage.error).not.toHaveBeenCalled()

    scope.status.value = 'completed'
    await vi.advanceTimersByTimeAsync(5000)
    expect(requests).toHaveLength(3)
  })

  it.each([
    { surface: 'user', session_id: 'browser-session', run: { run_id: 'old-run', status: 'running' } },
    { surface: 'admin', session_id: 'browser-session', run: { run_id: 'new-run', status: 'running' } },
    { surface: 'user', session_id: 'foreign-session', run: { run_id: 'new-run', status: 'running' } },
    { surface: 'user', session_id: 'browser-session', run: { run_id: 'new-run', status: 'completed' } },
  ])('服务端回读与当前运行不匹配时不恢复页面动作：%j', async session => {
    sessionLookup.mockResolvedValue(session)
    const requests: InternalAxiosRequestConfig[] = []
    http.defaults.adapter = async config => {
      requests.push(config)
      return response(config, 200, { code: 0, data: { actions: [] } })
    }
    const scope = mountBridge('user')
    scope.status.value = 'running'
    scope.runId.value = 'new-run'
    await vi.advanceTimersByTimeAsync(2050)
    expect(requests).toEqual([])
    expect(scope.navigate).not.toHaveBeenCalled()
  })

  it.each(['completed', 'cancelled', 'failed', 'incomplete', 'blocked', 'max_rounds_exceeded'])(
    '运行进入%s后，迟到的确认不恢复页面动作', async terminal => {
      let completeLookup!: (value: unknown) => void
      sessionLookup.mockImplementationOnce(() => new Promise(resolve => { completeLookup = resolve }))
      const requests: InternalAxiosRequestConfig[] = []
      http.defaults.adapter = async config => {
        requests.push(config)
        return response(config, 200, { code: 0, data: { actions: [] } })
      }
      const scope = mountBridge('user')
      scope.status.value = 'running'
      scope.runId.value = 'stopped-run'
      await vi.advanceTimersByTimeAsync(0)
      scope.status.value = terminal
      await vi.advanceTimersByTimeAsync(0)
      completeLookup({ surface: 'user', session_id: 'browser-session',
        run: { run_id: 'stopped-run', status: 'running' } })
      await vi.advanceTimersByTimeAsync(4100)
      expect(requests).toEqual([])
      expect(scope.navigate).not.toHaveBeenCalled()
    },
  )

  it('旧账号运行确认迟到时不发送恢复请求，新账号仍可继续确认', async () => {
    let completeLookup!: (value: unknown) => void
    sessionLookup.mockImplementationOnce(() => new Promise(resolve => { completeLookup = resolve }))
    const requests: InternalAxiosRequestConfig[] = []
    http.defaults.adapter = async config => {
      requests.push(config)
      return response(config, 200, { code: 0, data: { actions: [] } })
    }
    const scope = mountBridge('user')
    scope.status.value = 'running'
    scope.runId.value = 'old-run'
    await vi.advanceTimersByTimeAsync(0)
    actor.profile.id = 8
    actor.token = 'new-account-token'
    scope.runId.value = 'new-run'
    sessionLookup.mockResolvedValue({ surface: 'user', session_id: 'browser-session',
      run: { run_id: 'new-run', status: 'running' } })
    completeLookup({ surface: 'user', session_id: 'browser-session',
      run: { run_id: 'old-run', status: 'running' } })
    await vi.advanceTimersByTimeAsync(2100)
    expect(requests).toHaveLength(1)
    expect(requests[0]!.params.run_id).toBe('new-run')
    expect(scope.navigate).not.toHaveBeenCalled()
  })

  it('恢复的404仍拒绝，认领与回执的404仍报告真实错误', async () => {
    http.defaults.adapter = async config => rejectRequest(config, 404, 40400, '页面操作运行不存在')
    await expect(siteBrowserApi.pending({ session_id: 'browser-session', surface: 'user', run_id: 'missing-run' }))
      .rejects.toMatchObject({ code: 40400 })
    expect(ElMessage.error).not.toHaveBeenCalled()
    await expect(siteBrowserApi.claim(9, { session_id: 'browser-session', surface: 'user', client_nonce: 'nonce' }))
      .rejects.toMatchObject({ code: 40400 })
    await expect(siteBrowserApi.ack(9, {
      session_id: 'browser-session', surface: 'user', client_nonce: 'nonce', claim_token: 'claim', outcome: 'completed',
      observation: { snapshot_id: 'snapshot', route: '/forum', title: '', controls: [], messages: [] },
    })).rejects.toMatchObject({ code: 40400 })
    expect(ElMessage.error).toHaveBeenCalledTimes(2)
  })

  it('只读恢复的真实403权限错误仍报告给用户', async () => {
    http.defaults.adapter = async config => rejectRequest(config, 403, 40300, '当前账号没有该会话权限')
    await expect(siteBrowserApi.pending({ session_id: 'browser-session', surface: 'admin', run_id: 'foreign-run' }))
      .rejects.toMatchObject({ code: 40300 })
    expect(ElMessage.error).toHaveBeenCalledExactlyOnceWith('当前账号没有该会话权限')
  })
})
