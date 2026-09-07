import { describe, expect, it } from 'vitest'
import { OWASP_TOP10, SECURITY_CATALOG_METADATA, getOwaspByCode } from './owasp-knowledge'

describe('OWASP 官方当前分类知识', () => {
  it('十张卡片的名称、来源和中文示例完整且与 2025 语义一致', () => {
    expect(OWASP_TOP10).toHaveLength(10)
    expect(getOwaspByCode('A03')?.name_en).toBe('Software Supply Chain Failures')
    expect(getOwaspByCode('A05')?.name_en).toBe('Injection')
    expect(getOwaspByCode('A10')?.name_en).toBe('Mishandling of Exceptional Conditions')
    expect(getOwaspByCode('A01')?.cwe_refs).toContain('CWE-918')
    expect(getOwaspByCode('A10')?.cwe_refs).not.toContain('CWE-918')
    for (const item of OWASP_TOP10) {
      expect(item.source_url).toMatch(/^https:\/\/owasp.org\/Top10\/2025\//)
      expect(item.definition.length).toBeGreaterThan(20)
      expect(item.good_example.code.length).toBeGreaterThan(20)
      expect(item.bad_example.code.length).toBeGreaterThan(20)
      expect(item.prevention.length).toBeGreaterThan(2)
    }
  })

  it('映射计数来自实际列表，弱点总量不冒充 CVE 数量', () => {
    const mapped = OWASP_TOP10.flatMap((item) => item.cwe_refs)
    expect(new Set(mapped).size).toBe(249)
    expect(SECURITY_CATALOG_METADATA.cwe_weakness_count).toBe(944)
    expect(SECURITY_CATALOG_METADATA.mapped_weakness_count).toBe(246)
    expect(SECURITY_CATALOG_METADATA.cve_scope).toContain('不代表全量')
  })
})
