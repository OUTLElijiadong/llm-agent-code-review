/**
 * 中国漏洞库与国家标准来源入口(安全中心静态展示)
 *
 * 这里仅维护来源门户与标准参考，不包含 CNVD/CNNVD 数据同步、记录匹配或来源推断。
 * 只有业务数据带有对应官方记录编号或官方详情链接时，才能标记为该来源命中。
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
    name: 'CNVD',
    fullName: '国家信息安全漏洞共享平台',
    url: 'https://www.cnvd.org.cn/',
    operator: '国家互联网应急中心(CNCERT)',
    note: '独立的漏洞情报来源。仅在记录包含 CNVD 官方编号或官方详情链接时，才能标记为 CNVD 命中。',
  },
  {
    name: 'CNNVD',
    fullName: '中国国家信息安全漏洞库',
    url: 'https://www.cnnvd.org.cn/',
    operator: '中国信息安全测评中心',
    note: '独立的漏洞情报来源。仅在记录包含 CNNVD 官方编号或官方详情链接时，才能标记为 CNNVD 命中。',
  },
]

/** OWASP 可视化分类提示：仅作分类回退，不表示已查询或命中官方漏洞库。 */
export const OWASP_FALLBACK_NOTICE = 'OWASP 标签仅作回退分类；当前未接入 CNVD/CNNVD 官方匹配，无官方命中证据。'

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
    note: '国家标准中的漏洞分类分级参考；不代表 CNVD/CNNVD 记录匹配或扫描结果认证。',
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
