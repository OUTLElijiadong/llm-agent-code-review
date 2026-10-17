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

/** 相关参考规范；展示引用不代表平台认证或完整条款覆盖。 */
export interface CnStandard {
  code: string
  name: string
  note: string
}

export const CN_SECURITY_STANDARDS: CnStandard[] = [
  {
    code: 'GB/T 30279-2020',
    name: '信息安全技术 网络安全漏洞分类分级指南',
    note: '国家标准中的参考规范；平台内部扣分不等于该标准分级。引用不代表 CNVD/CNNVD 记录匹配；不代表认证或全部条款已覆盖。',
  },
  {
    code: 'GB/T 34943-2017',
    name: 'C/C++语言源代码漏洞测试规范',
    note: 'C/C++ 源代码漏洞测试参考规范；引用不代表认证或全部条款已覆盖。',
  },
  {
    code: 'GB/T 34944-2017',
    name: 'Java语言源代码漏洞测试规范',
    note: 'Java 源代码漏洞测试参考规范；引用不代表认证或全部条款已覆盖。',
  },
  {
    code: 'GB/T 34946-2017',
    name: 'C#语言源代码漏洞测试规范',
    note: 'C# 源代码漏洞测试参考规范；引用不代表认证或全部条款已覆盖。',
  },
]
