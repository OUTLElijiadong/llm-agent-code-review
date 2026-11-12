import { beforeEach, expect, it, vi } from 'vitest'

const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('./http', () => ({ ...http, default: http }))
import { getEnterpriseAssetRun, getEnterpriseAssetRunByRequest, runEnterpriseAsset } from './enterpriseAssetRuns'
import { getAgentRun } from './agentRunHistory'

beforeEach(() => { http.get.mockReset(); http.post.mockReset() })
it('请求和查询都绑定账号快照与企业全局发布来源', async () => {
  await runEnterpriseAsset(3, { request_key: 'request-1', input_text: '内容' }, 'token')
  expect(http.post).toHaveBeenCalledWith('/enterprise-asset-catalog/3/runs',
    { request_key: 'request-1', input_text: '内容' }, undefined, undefined, false, { authTokenSnapshot: 'token' })
  await getEnterpriseAssetRun(3, 7, 'token')
  expect(http.get).toHaveBeenLastCalledWith('/enterprise-asset-catalog/3/runs/7', undefined, undefined, false, { authTokenSnapshot: 'token' })
  await getEnterpriseAssetRunByRequest(3, 'request:1', 'token')
  expect(http.get).toHaveBeenLastCalledWith('/enterprise-asset-catalog/3/runs/by-request/request%3A1', undefined, undefined, false, { authTokenSnapshot: 'token' })
})
it('历史记录使用企业发布 ID 查询，避免与个人发布 ID 混淆', async () => {
  await getAgentRun({ id: 7, agent_id: null, publication_id: null, enterprise_publication_id: 3,
    version_number: 2, title: '资产', status: 'succeeded', actual_milli: 1, created_at: '' }, 'token')
  expect(http.get).toHaveBeenLastCalledWith('/enterprise-asset-catalog/3/runs/7', undefined, undefined, false, { authTokenSnapshot: 'token' })
})
