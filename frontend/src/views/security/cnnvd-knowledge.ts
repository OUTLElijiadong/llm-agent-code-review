/**
 * 中国漏洞库与国家标准对照知识(安全中心静态展示)
 *
 * 出处分层约定(与知识库治理规范一致):
 * - 官方来源:CNNVD/CNVD/NVDB 门户与国标号,见下方 PORTALS/STANDARDS。
 * - 平台整理:OWASP→CNNVD 26 类对照是平台依据 CNNVD《漏洞分类指南》的
 *   CWE 对照关系自行整理的工程映射,非 CNNVD 官方发布的对照表。
 */

/** 官方漏洞库门户 */
export interface CnPortal {
  name: string
  fullName: string
  url: string
  operator: string
  note: string
}

export const CN_VULN_PORTALS: CnPortal[] = [
  {
    name: 'CNNVD',
    fullName: '中国国家信息安全漏洞库',
    url: 'https://www.cnnvd.org.cn/',
    operator: '中国信息安全测评中心',
    note: '漏洞按 26 种类型归类并与 CWE 对照,危害等级分超危/高危/中危/低危四级;提供注册后 XML 全量/增量数据下载。',
  },
  {
    name: 'CNVD',
    fullName: '国家信息安全漏洞共享平台',
    url: 'https://www.cnvd.org.cn/',
    operator: '国家互联网应急中心(CNCERT)',
    note: '漏洞共享与周报发布平台,面向社会收录通用/事件型漏洞。',
  },
  {
    name: 'NVDB',
    fullName: '网络安全威胁和漏洞信息共享平台',
    url: 'https://www.miit.gov.cn/',
    operator: '工业和信息化部',
    note: '电信和互联网行业漏洞信息汇聚共享平台。',
  },
]

/** 相关国家标准 */
export interface CnStandard {
  code: string
  name: string
  note: string
}

export const CN_SECURITY_STANDARDS: CnStandard[] = [
  {
    code: 'GB/T 30279-2020',
    name: '网络安全漏洞分类分级指南',
    note: '规定漏洞分类与超危/高危/中危/低危四级分级方法,CNNVD 分级的国标依据。',
  },
  {
    code: 'GB/T 34943-2017',
    name: 'C/C++ 语言源代码安全测试规范',
    note: '源代码漏洞静态测试的国标规则集。',
  },
  {
    code: 'GB/T 34944-2017',
    name: 'Java 语言源代码漏洞测试规范',
    note: 'Java 源代码漏洞静态测试的国标规则集。',
  },
  {
    code: 'GB/T 34946-2017',
    name: 'C# 语言源代码漏洞测试规范',
    note: 'C# 源代码漏洞静态测试的国标规则集。',
  },
]

/**
 * OWASP Top 10 → CNNVD 漏洞类型对照(平台整理)。
 * key 为 OWASP 编码(A01~A10)。
 */
export const OWASP_TO_CNNVD_TYPE: Record<string, string> = {
  A01: '访问控制错误 / 授权问题',
  A02: '配置错误',
  A03: '设计错误(供应链,CNNVD 无单列类型)',
  A04: '加密问题',
  A05: '注入(SQL 注入 / 代码注入 / 命令注入)',
  A06: '设计错误',
  A07: '认证问题',
  A08: '数据伪造 / 代码问题',
  A09: '配置错误(日志与监控缺失,CNNVD 无单列类型)',
  A10: '代码问题 / 逻辑错误',
}

/** CNNVD 危害等级(GB/T 30279-2020) */
export const CNNVD_SEVERITY_LEVELS = ['超危', '高危', '中危', '低危'] as const
