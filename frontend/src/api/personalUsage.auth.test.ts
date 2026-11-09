import axios from 'axios'
import type { InternalAxiosRequestConfig } from 'axios'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/router', () => ({ default: { push: vi.fn(), replace: vi.fn() } }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { error: vi.fn() } }))

import http from './http'
import { getEnterpriseLimits, getEnterpriseUsage, setEnterpriseLimit } from './personalUsage'
import { listEnterpriseGroupMembers } from './enterprise'
import { clearToken, setToken } from '@/utils/token'

const originalAdapter = http.defaults.adapter
describe('企业用量请求认证快照', () => {
  afterEach(() => { clearToken(); http.defaults.adapter = originalAdapter })

  it.each([
    ['额度保存', () => setEnterpriseLimit(10, { period: '2026-11', scope: 'enterprise', limit_credits: 25 }, 'session-a')],
    ['用量查询', () => getEnterpriseUsage(10, 30, 3, 1, 'session-a')],
    ['额度查询', () => getEnterpriseLimits(10, '2026-11', 3, 'session-a')],
    ['小组成员查询', () => listEnterpriseGroupMembers(10, 3, 'session-a')],
  ])('%s 在发送前切换账号时取消，而不使用新账号发送', async (_label, request) => {
    const adapter = vi.fn(async (config: InternalAxiosRequestConfig) => ({ data: { code: 0, message: '', data: {} }, status: 200,
      statusText: 'OK', headers: {}, config }))
    http.defaults.adapter = adapter
    setToken('session-a')
    const pending = request()
    setToken('session-b')
    await expect(pending).rejects.toSatisfy(axios.isCancel)
    expect(adapter).not.toHaveBeenCalled()
  })

  it('认证快照未变化时按原账号提交额度', async () => {
    const adapter = vi.fn(async (config: InternalAxiosRequestConfig) => ({ data: { code: 0, message: '', data: {} }, status: 200,
      statusText: 'OK', headers: {}, config }))
    http.defaults.adapter = adapter
    setToken('session-a')
    await setEnterpriseLimit(10, { period: '2026-11', scope: 'enterprise', limit_credits: 25 }, 'session-a')
    expect(adapter).toHaveBeenCalledOnce()
    expect(adapter.mock.calls[0][0].headers.Authorization).toBe('Bearer session-a')
  })
})
