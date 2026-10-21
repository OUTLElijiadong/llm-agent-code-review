import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { CN_VULN_PORTALS } from './cnnvd-knowledge'

const mocks = vi.hoisted(() => ({
  checklist: vi.fn(),
  dashboard: vi.fn(),
  vulnerabilityRecords: vi.fn(),
  userStore: { token: 'token-a', profile: { id: 1 }, hasPermission: vi.fn(() => false) },
}))
vi.mock('@/api/security', () => ({
  getSecurityChecklist: mocks.checklist,
  getSecurityDashboard: mocks.dashboard,
  getVulnerabilityRecords: mocks.vulnerabilityRecords,
}))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('@/stores/user', async () => {
  const { reactive } = await import('vue')
  Object.assign(mocks, { userStore: reactive(mocks.userStore) })
  return { useUserStore: () => mocks.userStore }
})
import SecurityCenter from './SecurityCenter.vue'
import { CN_SECURITY_STANDARDS } from './cnnvd-knowledge'

const emptyChecklist = { owasp_top10: [], secret_patterns: [], static_rules: [] }
const dashboard = { project_count: 7, scanned_project_count: 0, avg_risk_score: null }
const connectedSources = [
  { code: 'cnvd', name: 'CNVD', integration: 'implemented', status: 'available', connected: true,
    status_message: '官方查询成功', checked_at: '2026-10-21T15:30:00Z', cached: false,
    query_scope: 'latest_page', coverage_scope: null, coverage_label: '', primary_status: null,
    primary_http_status: null, http_status: 200, record_count: 1, evidence: [] },
  { code: 'cnnvd', name: 'CNNVD', integration: 'implemented', status: 'blocked', connected: false,
    status_message: '官方源业务拒绝（码 4030）；本次查询未成功。', checked_at: '2026-10-21T15:30:00Z',
    cached: false, query_scope: 'official_search_first_page', coverage_scope: null, coverage_label: '',
    primary_status: null, primary_http_status: null, http_status: 200,
    upstream_code: '4030', record_count: 0, evidence: [] },
]
const vulnerabilityQuery = { query: '', sources: connectedSources, records: [
  { source: 'cnvd', record_id: 'CNVD-2026-12345', title: '经后端核验的测试记录',
    url: 'https://www.cnvd.org.cn/flaw/show/CNVD-2026-12345', checked_at: '2026-10-21T15:30:00Z',
    match_type: 'latest', evidence_url: 'https://www.cnvd.org.cn/flaw/list', evidence_sha256: 'a'.repeat(64),
    description: '', coverage_scope: null, source_date: null, severity: '', cve_ids: [], cwe_ids: [], published_at: null, updated_at: null },
] }
function mountPage() {
  return mount(SecurityCenter, { global: {
    directives: { loading: () => {} },
    stubs: {
      SecurityScanModal: true,
      'el-input': {
        inheritAttrs: false,
        props: ['modelValue'],
        emits: ['update:modelValue'],
        template: '<input v-bind="$attrs" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
      },
      'el-icon': { template: '<span><slot /></span>' },
      'el-button': { props: ['disabled'], template: '<button :disabled="disabled"><slot /></button>' },
      'el-dialog': true,
      'el-table': true,
      'el-table-column': true,
      'el-tag': true,
    },
  } })
}
beforeEach(() => {
  mocks.userStore.token = 'token-a'
  mocks.userStore.profile = { id: 1 }
  mocks.userStore.hasPermission.mockReset().mockReturnValue(false)
  mocks.checklist.mockReset().mockResolvedValue(emptyChecklist)
  mocks.dashboard.mockReset().mockResolvedValue(dashboard)
  mocks.vulnerabilityRecords.mockReset().mockResolvedValue(vulnerabilityQuery)
})

describe('安全中心独立读取恢复', () => {
  it('展示真实CNVD行业归档记录、覆盖边界和主库521，不把来源日期称为发布日期', async () => {
    const archivedRecords = [
      {
        source: 'cnvd', record_id: 'CNVD-2026-41250', title: 'IBM Langflow OSS访问控制不当漏洞',
        url: 'https://www.cnvd.org.cn/flaw/show/CNVD-2026-41250', description: '', severity: '中',
        cve_ids: [], cwe_ids: [], published_at: null, updated_at: null, checked_at: '2026-10-21T16:56:02.165167+00:00',
        match_type: 'latest', source_date: '2026-10-12', coverage_scope: 'ai_sector',
        evidence_url: 'https://ai.cnvd.org.cn/',
        evidence_sha256: 'b4975671f66df73ea7b4822d645a7e7e88463f53ae25d60c865f5d35046d21fd',
      },
      {
        source: 'cnvd', record_id: 'CNVD-2026-41251', title: 'IBM Langflow OSS代码注入漏洞',
        url: 'https://www.cnvd.org.cn/flaw/show/CNVD-2026-41251', description: '', severity: '高',
        cve_ids: [], cwe_ids: [], published_at: null, updated_at: null, checked_at: '2026-10-21T16:56:02.165167+00:00',
        match_type: 'latest', source_date: '2026-10-12', coverage_scope: 'ai_sector',
        evidence_url: 'https://ai.cnvd.org.cn/',
        evidence_sha256: 'b4975671f66df73ea7b4822d645a7e7e88463f53ae25d60c865f5d35046d21fd',
      },
    ]
    mocks.vulnerabilityRecords.mockResolvedValueOnce({
      query: '',
      sources: [
        {
          ...connectedSources[0], coverage_scope: 'ai_sector',
          coverage_label: '人工智能行业子库首页（最多20条），非CNVD全库',
          primary_status: 'blocked', primary_http_status: 521,
          status_message: '已取得人工智能行业子库首页记录（最多20条），非CNVD全库；主库HTTP521仍受限。',
          evidence: [
            { url: 'https://www.cnvd.org.cn/flaw/list', method: 'GET', checked_at: '2026-10-21T16:56:02.197350+00:00',
              http_status: 521, response_sha256: '1d7a96bd0998e00d0748be63d255c0551518d9e47bc655bbb70a042a85814b15',
              response_bytes: 946, cached: false },
            { url: 'https://ai.cnvd.org.cn/', method: 'GET', checked_at: '2026-10-21T16:56:04.677798+00:00',
              http_status: 200, response_sha256: 'b4975671f66df73ea7b4822d645a7e7e88463f53ae25d60c865f5d35046d21fd',
              response_bytes: 18475, cached: false },
          ],
        },
        connectedSources[1],
      ],
      records: archivedRecords,
    })

    const page = mountPage()
    await flushPromises()
    const status = page.get('[data-testid="cn-source-status"]').text()
    const records = page.get('[data-testid="cn-vulnerability-records"]').text()
    expect(status).toContain('人工智能行业子库首页（最多20条），非CNVD全库')
    expect(status).toContain('主库：访问受阻，HTTP 521')
    expect(status).toContain('本次请求证据')
    expect(records).toContain('CNVD-2026-41250')
    expect(records).toContain('CNVD-2026-41251')
    expect(records).toContain('来源日期：2026-10-12')
    expect(records).not.toContain('发布日期：2026-10-12')
    expect(page.findAll('a').some(link => link.attributes('href') === 'https://ai.cnvd.org.cn/')).toBe(true)
    page.unmount()
  })

  it('如实标记后端返回的缓存证据', async () => {
    mocks.vulnerabilityRecords.mockResolvedValueOnce({
      ...vulnerabilityQuery,
      sources: [{
        ...connectedSources[0],
        evidence: [{ url: 'https://www.cnvd.org.cn/flaw/list', method: 'GET', checked_at: '2026-10-21T15:30:00Z',
          http_status: 200, upstream_code: null, response_sha256: 'a'.repeat(64), response_bytes: 128,
          cached: true, retry_after_seconds: null }],
      }, connectedSources[1]],
    })
    const page = mountPage()
    await flushPromises()
    expect(page.get('[data-testid="cn-source-status"]').text()).toContain('缓存证据')
    page.unmount()
  })

  it('仅ai_sector覆盖允许固定ai.cnvd.org.cn证据主机，拒绝其他CNVD子域', async () => {
    const base = vulnerabilityQuery.records[0]
    mocks.vulnerabilityRecords.mockResolvedValueOnce({
      ...vulnerabilityQuery,
      records: [
        { ...base, record_id: 'CNVD-AI-OK', coverage_scope: 'ai_sector', evidence_url: 'https://ai.cnvd.org.cn/',
          evidence_sha256: 'b'.repeat(64) },
        { ...base, record_id: 'CNVD-ARBITRARY-SUBDOMAIN', coverage_scope: 'ai_sector',
          evidence_url: 'https://other.cnvd.org.cn/', evidence_sha256: 'c'.repeat(64) },
        { ...base, record_id: 'CNVD-NO-SCOPE', coverage_scope: null,
          evidence_url: 'https://ai.cnvd.org.cn/', evidence_sha256: 'd'.repeat(64) },
      ],
    })
    const page = mountPage()
    await flushPromises()
    const records = page.get('[data-testid="cn-vulnerability-records"]').text()
    expect(records).toContain('CNVD-AI-OK')
    expect(records).not.toContain('CNVD-ARBITRARY-SUBDOMAIN')
    expect(records).not.toContain('CNVD-NO-SCOPE')
    expect(page.get('[data-testid="cn-vulnerability-records"]').text()).toContain('2 条记录因来源状态或官方链接证据不完整而隐藏')
    page.unmount()
  })

  it('概览失败保留常驻错误和请求ID，不影响知识库并能真实重试', async () => {
    mocks.dashboard.mockRejectedValueOnce({ message: '上游暂不可用', request_id: 'req-security-1', next_action: '稍后重试读取' })
    const page = mountPage()
    await flushPromises()
    expect(page.get('[data-testid="dashboard-error"]').text()).toContain('上游暂不可用')
    expect(page.get('[data-testid="dashboard-error"]').text()).toContain('req-security-1')
    expect(page.text()).toContain('OWASP Top 10')
    expect(page.text()).not.toContain('还没有项目可分析')
    await page.get('[data-testid="refresh-dashboard"]').trigger('click')
    await flushPromises()
    expect(mocks.dashboard).toHaveBeenCalledTimes(2)
    expect(page.find('[data-testid="dashboard-error"]').exists()).toBe(false)
    expect(page.text()).toContain('7 个项目还未进行安全审计')
    page.unmount()
  })

  it('清单失败不能把未知数量显示成0，重试成功的空清单才显示0与空态', async () => {
    mocks.checklist.mockRejectedValueOnce(new Error('网络中断'))
    const page = mountPage()
    await flushPromises()
    expect(page.get('[data-testid="checklist-error"]').text()).toContain('网络中断')
    expect(page.findAll('.hl').map(item => item.text())).toEqual(['—', '—'])
    expect(page.text()).toContain('7 个项目还未进行安全审计')
    await page.get('[data-testid="refresh-checklist"]').trigger('click')
    await flushPromises()
    expect(page.findAll('.hl').map(item => item.text())).toEqual(['0', '0'])
    expect(page.text()).toContain('未配置敏感信息规则')
    expect(page.text()).toContain('未配置静态语义规则')
    page.unmount()
  })

  it('重复点击重试只发一个请求，结束后恢复按钮', async () => {
    let resolve!: (value: typeof dashboard) => void
    mocks.dashboard.mockReturnValue(new Promise(done => { resolve = done }))
    const page = mountPage()
    await flushPromises()
    const button = page.get('[data-testid="refresh-dashboard"]')
    expect(button.attributes('disabled')).toBeDefined()
    await button.trigger('click')
    expect(mocks.dashboard).toHaveBeenCalledTimes(1)
    resolve(dashboard)
    await flushPromises()
    expect(button.attributes('disabled')).toBeUndefined()
    page.unmount()
  })

  it.each([40101, 40301, 40401])('失权或资源消失 %s 清除旧概览', async code => {
    const page = mountPage()
    await flushPromises()
    expect(page.text()).toContain('7 个项目还未进行安全审计')
    mocks.dashboard.mockRejectedValueOnce({ code, message: '无法访问' })
    await page.get('[data-testid="refresh-dashboard"]').trigger('click')
    await flushPromises()
    expect(page.text()).not.toContain('7 个项目还未进行安全审计')
    expect(page.get('[data-testid="dashboard-error"]').text()).toContain('无法访问')
    page.unmount()
  })

  it('暂时错误保留上次成功结果并明确未更新', async () => {
    const page = mountPage()
    await flushPromises()
    mocks.dashboard.mockRejectedValueOnce(new Error('连接超时'))
    await page.get('[data-testid="refresh-dashboard"]').trigger('click')
    await flushPromises()
    expect(page.text()).toContain('7 个项目还未进行安全审计')
    expect(page.get('[data-testid="dashboard-error"]').text()).toContain('保留上次成功结果')
    expect(page.get('[data-testid="dashboard-error"]').text()).toContain('尚未更新')
    page.unmount()
  })
})

describe('国内参考规范展示边界', () => {
  it('国标名称采用官方标题，不将源代码漏洞测试误称为安全测试', () => {
    expect(CN_SECURITY_STANDARDS.map(({ code, name }) => ({ code, name }))).toEqual([
      { code: 'GB/T 30279-2020', name: '信息安全技术 网络安全漏洞分类分级指南' },
      { code: 'GB/T 34943-2017', name: 'C/C++语言源代码漏洞测试规范' },
      { code: 'GB/T 34944-2017', name: 'Java语言源代码漏洞测试规范' },
      { code: 'GB/T 34946-2017', name: 'C#语言源代码漏洞测试规范' },
    ])
  })

  it.each(CN_SECURITY_STANDARDS)('$code 明示参考规范，引用不能证明认证或全部条款覆盖', ({ note }) => {
    expect(note).toContain('参考规范')
    expect(note).toContain('不代表认证或全部条款已覆盖')
    expect(note).not.toContain('国标规则集')
  })
})

describe('安全分类来源标识', () => {
  it('先列 CNVD 与 CNNVD，并分别保留各自官方入口', () => {
    expect(CN_VULN_PORTALS.map(portal => portal.name)).toEqual(['CNVD', 'CNNVD'])
    expect(CN_VULN_PORTALS.slice(0, 2).map(portal => portal.name)).toEqual(['CNVD', 'CNNVD'])
    expect(CN_VULN_PORTALS.find(portal => portal.name === 'CNVD')?.url).toBe('https://www.cnvd.org.cn/')
    expect(CN_VULN_PORTALS.find(portal => portal.name === 'CNNVD')?.url).toBe('https://www.cnnvd.org.cn/')
  })

  it('读取后端逐源状态和真实记录，不把来源失败当成无匹配', async () => {
    const page = mountPage()
    await flushPromises()

    const sourceStatus = page.get('[data-testid="cn-source-status"]')
    expect(mocks.vulnerabilityRecords).toHaveBeenCalledWith({ q: '', source: 'all', limit: 10 })
    expect(sourceStatus.text()).toContain('CNVD：已连接')
    expect(sourceStatus.text()).toContain('CNNVD：访问受阻')
    expect(sourceStatus.text()).toContain('4030')
    expect(sourceStatus.text()).toContain('OWASP 分类参考')
    expect(sourceStatus.text()).not.toContain('OWASP 回退分类')
    expect(sourceStatus.text()).not.toContain('无匹配')
    expect(sourceStatus.text()).toContain('2026-10-21')
    expect(page.get('[data-testid="cn-vulnerability-records"]').text()).toContain('经后端核验的测试记录')
    expect(page.get('[data-testid="cn-vulnerability-records"]').get('a').attributes('href'))
      .toBe('https://www.cnvd.org.cn/flaw/show/CNVD-2026-12345')
    expect(page.text()).not.toContain('CNNVD 26 类')
    expect(page.find('.cn-mapping-table').exists()).toBe(false)
    expect(page.get('.cn-portal-card').attributes('href')).toBe('https://www.cnvd.org.cn/')
    expect(page.findAll('.cn-portal-card')[1].attributes('href')).toBe('https://www.cnnvd.org.cn/')
    const sectionTitles = page.findAll('.block-title').map(title => title.text())
    expect(sectionTitles.findIndex(title => title.includes('中国漏洞库来源')))
      .toBeLessThan(sectionTitles.findIndex(title => title.includes('OWASP Top 10')))
    expect(page.text()).toContain('风险分类参考，不代表 CNVD/CNNVD 命中')
    expect(page.text()).not.toContain('按 OWASP 回退')
    expect(sectionTitles.find(title => title.includes('OWASP Top 10'))).not.toContain('回退分类')
    page.unmount()
  })

  it('OWASP 热点只作分类参考，并使用后端逐源状态说明核验边界', async () => {
    mocks.dashboard.mockResolvedValueOnce({
      ...dashboard,
      scanned_project_count: 1,
      owasp_hotspots: [{ owasp: 'A01:2025', count: 3 }],
    })
    const page = mountPage()
    await flushPromises()

    expect(page.get('.ov-hot-title').text()).toContain('OWASP 分类参考')
    expect(page.get('.ov-hot-title').text()).toContain('CNVD：已连接')
    expect(page.get('.ov-hot-title').text()).toContain('CNNVD：访问受阻')
    expect(page.get('.ov-hot-title').text()).not.toContain('确认无匹配')
    expect(page.get('.ov-hot-name').text()).toBe('A01:2025')
    page.unmount()
  })

  it('按后端契约显式搜索；成功空结果不伪造匹配或全库无命中', async () => {
    const page = mountPage()
    await flushPromises()
    mocks.vulnerabilityRecords.mockResolvedValueOnce({
      query: 'CVE-2026-1111',
      sources: connectedSources.map(source => ({ ...source, status: 'available', connected: true, record_count: 0,
        query_scope: source.code === 'cnvd' ? 'latest_page_filter' : 'official_search_first_page' })),
      records: [],
    })
    const input = page.get('[data-testid="vulnerability-query"]')
    await input.setValue('CVE-2026-1111')
    await page.get('[data-testid="search-vulnerabilities"]').trigger('click')
    await flushPromises()

    expect(mocks.vulnerabilityRecords).toHaveBeenLastCalledWith({ q: 'CVE-2026-1111', source: 'all', limit: 10 })
    const emptyResultMessage = page.get('[data-testid="cn-vulnerability-records"]').text()
    expect(emptyResultMessage).toContain('本次查询未返回记录')
    expect(emptyResultMessage).toContain('latest_page_filter')
    expect(emptyResultMessage).toContain('不代表官方库全库无匹配')
    expect(emptyResultMessage).not.toContain('OWASP')
    expect(page.get('[data-testid="cn-source-status"]').text()).not.toContain('OWASP 回退分类')
    page.unmount()
  })

  it('结构完整的 empty 来源显示未返回匹配记录，不标记为已连接', async () => {
    mocks.vulnerabilityRecords.mockResolvedValueOnce({
      query: 'CVE-2026-1111',
      sources: connectedSources.map(source => source.code === 'cnvd'
        ? { ...source, status: 'empty', connected: false, record_count: 0 }
        : source),
      records: [],
    })
    const page = mountPage()
    await flushPromises()

    const sourceStatus = page.get('[data-testid="cn-source-status"]').text()
    expect(sourceStatus).toContain('CNVD：未返回匹配记录')
    expect(sourceStatus).not.toContain('CNVD：已连接')
    expect(page.get('[data-testid="cn-vulnerability-records"]').text()).toContain('未返回匹配记录')
    page.unmount()
  })

  it('账号切换时隔离旧请求结果并加载新账号作用域', async () => {
    const pending: Array<{
      token: string
      resolve: (value: typeof vulnerabilityQuery) => void
    }> = []
    mocks.vulnerabilityRecords.mockImplementation(() => new Promise(resolve => {
      pending.push({ token: mocks.userStore.token, resolve })
    }))
    const page = mountPage()
    await flushPromises()
    const oldRequestCount = pending.length

    mocks.userStore.token = 'token-b'
    mocks.userStore.profile = { id: 2 }
    await flushPromises()
    expect(pending.some(request => request.token === 'token-b')).toBe(true)

    const currentQuery = {
      ...vulnerabilityQuery,
      query: 'new-account-result',
      records: [{ ...vulnerabilityQuery.records[0], record_id: 'CNVD-2026-22222', title: '新账号记录' }],
    }
    pending.filter(request => request.token !== 'token-b').forEach(request => request.resolve(vulnerabilityQuery))
    await flushPromises()
    expect(page.text()).not.toContain('CNVD-2026-12345')

    pending.filter(request => request.token === 'token-b').forEach(request => request.resolve(currentQuery))
    await flushPromises()
    expect(pending.length).toBeGreaterThan(oldRequestCount)
    expect(page.text()).toContain('CNVD-2026-22222')
    expect(page.text()).not.toContain('CNVD-2026-12345')
    page.unmount()
  })

  it('卸载后完成的查询不继续更新页面状态', async () => {
    let resolve!: (value: typeof vulnerabilityQuery) => void
    mocks.vulnerabilityRecords.mockReturnValueOnce(new Promise(done => { resolve = done }))
    const page = mountPage()
    const setupState = (page.vm as unknown as { $: { setupState: Record<string, unknown> } }).$.setupState
    expect(setupState.vulnerabilityData).toBeNull()
    page.unmount()

    resolve(vulnerabilityQuery)
    await flushPromises()
    expect(setupState.vulnerabilityData).toBeNull()
  })

  it('来源失败或记录证据不属于官方域名时不展示为漏洞命中', async () => {
    const page = mountPage()
    await flushPromises()
    const unverifiedDomainRecord = {
      ...vulnerabilityQuery.records[0],
      record_id: 'FAKE-OWASP-A01',
      title: '非官方域名记录',
      url: 'https://example.com/fake-record',
    }
    const failedSourceRecord = {
      ...vulnerabilityQuery.records[0],
      source: 'cnnvd',
      record_id: 'CNNVD-FAKE-2',
      title: '来源失败记录',
      url: 'https://www.cnnvd.org.cn/web/vulninfo/show/CNNVD-FAKE-2',
      evidence_url: 'https://www.cnnvd.org.cn/web/vulninfo/show/CNNVD-FAKE-2',
    }
    const invalidDigestRecord = {
      ...vulnerabilityQuery.records[0],
      record_id: 'CNVD-FAKE-3',
      title: '证据摘要格式错误',
      evidence_sha256: 'not-a-sha256',
    }
    const nonstandardPortRecord = {
      ...vulnerabilityQuery.records[0],
      record_id: 'CNVD-FAKE-4',
      title: '非标准端口记录',
      url: 'https://www.cnvd.org.cn:8443/flaw/show/CNVD-FAKE-4',
      evidence_url: 'https://www.cnvd.org.cn:8443/flaw/show/CNVD-FAKE-4',
    }
    const unknownSourceRecord = {
      ...vulnerabilityQuery.records[0],
      source: 'untrusted-source',
      record_id: 'CNNVD-FAKE-UNKNOWN-SOURCE',
      title: '来源未知记录',
      url: 'https://www.cnnvd.org.cn/web/vulninfo/show/CNNVD-FAKE-UNKNOWN-SOURCE',
      evidence_url: 'https://www.cnnvd.org.cn/web/vulninfo/show/CNNVD-FAKE-UNKNOWN-SOURCE',
    }
    mocks.vulnerabilityRecords.mockResolvedValueOnce({
      query: 'A01',
      sources: [
        ...connectedSources.map(source => source.code === 'cnnvd'
          ? { ...source, status: 'blocked', connected: false }
          : source),
        { ...connectedSources[1], code: 'untrusted-source', status: 'available', connected: true },
      ],
      records: [unverifiedDomainRecord, failedSourceRecord, invalidDigestRecord, nonstandardPortRecord, unknownSourceRecord],
    })
    await page.get('[data-testid="refresh-cn-sources"]').trigger('click')
    await flushPromises()

    const results = page.get('[data-testid="cn-vulnerability-records"]')
    expect(results.text()).toContain('当前展示 0 条可核验记录')
    expect(results.text()).toContain('5 条记录因来源状态或官方链接证据不完整而隐藏')
    expect(results.text()).not.toContain('FAKE-OWASP-A01')
    expect(results.text()).not.toContain('CNNVD-FAKE-2')
    expect(results.text()).not.toContain('CNVD-FAKE-3')
    expect(results.text()).not.toContain('CNVD-FAKE-4')
    expect(results.text()).not.toContain('CNNVD-FAKE-UNKNOWN-SOURCE')
    expect(results.text()).not.toContain('OWASP 回退')
    page.unmount()
  })

  it('平台查询失败如实提示，不把旧记录显示成刚查询结果', async () => {
    const page = mountPage()
    await flushPromises()
    mocks.vulnerabilityRecords.mockRejectedValueOnce(new Error('security:view 已失效'))
    await page.get('[data-testid="refresh-cn-sources"]').trigger('click')
    await flushPromises()
    expect(page.get('[data-testid="cn-query-error"]').text()).toContain('security:view 已失效')
    expect(page.get('[data-testid="cn-vulnerability-records"]').text()).toContain('尚未取得本次查询结果')
    page.unmount()
  })
})
