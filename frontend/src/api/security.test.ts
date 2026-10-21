import { beforeEach, describe, expect, it, vi } from 'vitest'

const httpApi = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('./http', () => ({ get: httpApi.get, post: httpApi.post }))

import {
  getVulnerabilityRecords,
  getVulnerabilitySources,
  type VulnerabilitySourceQueryOut,
} from './security'

describe('国内漏洞源只读 API 契约', () => {
  beforeEach(() => {
    httpApi.get.mockReset().mockResolvedValue([])
  })

  it('读取来源状态使用受权限保护的后端路由', async () => {
    const sources = [{ code: 'cnvd', status: 'blocked', connected: false }]
    httpApi.get.mockResolvedValueOnce(sources)
    await expect(getVulnerabilitySources()).resolves.toBe(sources)
    expect(httpApi.get).toHaveBeenCalledWith('/security/vulnerability-sources')
  })

  it('检索词、来源和每源数量按后端契约传参，不拼接非正式路由', async () => {
    const result = { query: 'CVE-2026-1111', sources: [], records: [] }
    httpApi.get.mockResolvedValueOnce(result)
    const params = { q: 'CVE-2026-1111', source: 'cnnvd' as const, limit: 5 }
    await expect(getVulnerabilityRecords(params)).resolves.toBe(result)
    expect(httpApi.get).toHaveBeenCalledWith('/security/vulnerability-records', params)
    expect(httpApi.get).not.toHaveBeenCalledWith('/security/vulnerabilities', expect.anything())
    expect(httpApi.post).not.toHaveBeenCalled()
  })

  it('上游状态与结果由后端响应透传，API 层不把空记录改写为成功命中', async () => {
    const result = {
      query: '',
      sources: [{ code: 'cnnvd', status: 'blocked', connected: false,
        status_message: '官方源业务拒绝（码 4030）；本次查询未成功。' }],
      records: [],
    }
    httpApi.get.mockResolvedValueOnce(result)
    await expect(getVulnerabilityRecords({ source: 'all', limit: 10 })).resolves.toEqual(result)
    expect(httpApi.get).toHaveBeenCalledWith('/security/vulnerability-records', { source: 'all', limit: 10 })
  })

  it('保留后端行业范围、主库521、缓存证据及来源日期字段', async () => {
    const result: VulnerabilitySourceQueryOut = {
      query: '',
      sources: [{
        code: 'cnvd', name: 'CNVD', role: 'vulnerability_intelligence', url: 'https://www.cnvd.org.cn/',
        integration: 'implemented', adapter: 'official_public_web', status: 'available', connected: true,
        status_message: '已取得人工智能行业子库首页记录（最多20条），非CNVD全库；主库HTTP521仍受限。',
        checked_at: '2026-10-21T16:56:02.165167+00:00', last_success_at: '2026-10-21T16:56:02.165167+00:00',
        http_status: 200, upstream_code: null, record_count: 1, cached: false, query_scope: 'ai_sector_latest_page',
        coverage_scope: 'ai_sector', coverage_label: '人工智能行业子库首页（最多20条），非CNVD全库',
        primary_status: 'blocked', primary_http_status: 521, last_query: '', retry_after_seconds: null,
        evidence: [{ url: 'https://ai.cnvd.org.cn/', method: 'GET', checked_at: '2026-10-21T16:56:04.677798+00:00',
          http_status: 200, upstream_code: null, response_sha256: 'b'.repeat(64), response_bytes: 18475,
          cached: true, retry_after_seconds: null }],
      }],
      records: [{
        source: 'cnvd', record_id: 'CNVD-2026-41250', title: 'IBM Langflow OSS访问控制不当漏洞',
        url: 'https://www.cnvd.org.cn/flaw/show/CNVD-2026-41250', description: '', coverage_scope: 'ai_sector',
        source_date: '2026-10-12', severity: '中', cve_ids: [], cwe_ids: [], published_at: null, updated_at: null,
        checked_at: '2026-10-21T16:56:02.165167+00:00', match_type: 'latest',
        evidence_url: 'https://ai.cnvd.org.cn/', evidence_sha256: 'b'.repeat(64),
      }],
    }
    httpApi.get.mockResolvedValueOnce(result)
    await expect(getVulnerabilityRecords({ source: 'cnvd', limit: 1 })).resolves.toBe(result)
    expect(httpApi.get).toHaveBeenCalledWith('/security/vulnerability-records', { source: 'cnvd', limit: 1 })
  })
})
