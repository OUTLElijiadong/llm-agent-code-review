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
  const sessionId = ref('browser-session'), runId = ref(''), status = ref('')
  const currentRoute = ref({ fullPath: '/forum' })
  const navigate = vi.fn(async (path: string) => { currentRoute.value.fullPath = path })
  wrappers.push(mount(defineComponent({
    setup() {
      useSiteBrowserBridge({
        surface,
        sessionId: () => sessionId.value,
        runId: () => runId.value,
        status: () => status.value,
        router: { currentRoute, push: navigate } as unknown as Router,
      })
      return () => h('main')
    },
  })))
  return { sessionId, runId, status, navigate }
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  sessionStorage.clear()
  actor.profile.id = 7
  actor.token = 'browser-owner-token'
})
afterEach(() => {
  for (const wrapper of wrappers.splice(0)) wrapper.unmount()
  http.defaults.adapter = originalAdapter
  vi.useRealTimers()
})

describe('页面桥的运行创建与恢复时序', () => {
  it.each(['user', 'admin'] as const)('客户端先发布 %s run ID 时，未落库的恢复不弹错且落库后执行排队动作', async surface => {
    let committed = false
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
    expect(requests).toHaveLength(1)
    expect(requests[0]!.params).toEqual({ session_id: 'browser-session', surface, run_id: action.run_id })
    expect(scope.navigate).not.toHaveBeenCalled()
    expect(ElMessage.error).not.toHaveBeenCalled()

    committed = true
    await vi.advanceTimersByTimeAsync(2080)
    expect(requests.map(request => request.url)).toEqual([
      '/agent-browser/pending', '/agent-browser/pending',
      '/agent-browser/actions/9/claim', '/agent-browser/actions/9/ack',
    ])
    expect(requests[1]!.params).toEqual(requests[0]!.params)
    expect(JSON.parse(requests[2]!.data)).toEqual({ session_id: 'browser-session', surface, client_nonce: 'owner-nonce' })
    expect(scope.navigate).toHaveBeenCalledExactlyOnceWith('/forum/42')
    expect(JSON.parse(requests[3]!.data)).toMatchObject({
      session_id: 'browser-session', surface, client_nonce: 'owner-nonce', claim_token: 'claim-9', outcome: 'completed',
      observation: { snapshot_id: 'snapshot-9', route: '/forum/42' },
    })
    expect(ElMessage.error).not.toHaveBeenCalled()

    scope.status.value = 'completed'
    await vi.advanceTimersByTimeAsync(5000)
    expect(requests).toHaveLength(4)
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
