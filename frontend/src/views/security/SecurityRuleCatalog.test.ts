import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { SecurityRuleCatalogOut } from '@/types/security'

const mocks = vi.hoisted(() => ({ getCatalog: vi.fn() }))
vi.mock('@/api/security', () => ({ getSecurityRuleCatalog: mocks.getCatalog }))

import SecurityRuleCatalog from './SecurityRuleCatalog.vue'

const catalog: SecurityRuleCatalogOut = {
  items: [],
  counts: {},
  engines: [],
  sources: [
    { code: 'cnvd', name: 'CNVD', role: '官方漏洞库', url: 'https://www.cnvd.org.cn/' },
    { code: 'cnnvd', name: 'CNNVD', role: '官方漏洞库', url: 'https://www.cnnvd.org.cn/' },
  ],
  mapping_note: '规则目录不表示官方记录命中。',
}

describe('SecurityRuleCatalog OWASP 来源说明', () => {
  beforeEach(() => mocks.getCatalog.mockReset().mockResolvedValue(catalog))

  it('明确 CWE/OWASP 是分类参考，且未接入官方匹配', async () => {
    const wrapper = mount(SecurityRuleCatalog, {
      global: {
        directives: { loading: { mounted: () => undefined, updated: () => undefined } },
        stubs: {
          ...Object.fromEntries([
          'el-button', 'el-card', 'el-input', 'el-option', 'el-select', 'el-table', 'el-table-column', 'el-tag',
          ].map(name => [name, { template: '<div><slot /><slot name="default" /></div>' }])),
          'el-table-column': { template: '<div />' },
        },
      },
    })
    await flushPromises()
    expect(wrapper.find('.owasp-mapping-note').text()).toContain('CWE/OWASP 是规则分类参考')
    expect(wrapper.find('.owasp-mapping-note').text()).toContain('CNVD/CNNVD 官方漏洞库当前未查询')
    expect(wrapper.find('.owasp-mapping-note').text()).toContain('未查询不代表官方库无匹配')
    wrapper.unmount()
  })
})
