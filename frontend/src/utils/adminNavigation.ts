export const ADMIN_NAVIGATION_GROUPS = [
  {
    key: 'platform',
    title: '平台治理',
    paths: [
      '/admin/governance', '/admin/operations', '/admin/security-center', '/admin/access',
      '/admin/platform', '/admin/enterprise-asset-publications',
    ],
  },
  {
    key: 'billing',
    title: '计费与对账',
    paths: [
      '/admin/billing-refunds', '/admin/billing-statements', '/admin/billing-reservations',
      '/admin/billing-reviews', '/admin/billing-rate-reviews', '/admin/billing-refund-reviews',
      '/admin/billing-pricing', '/billing',
    ],
  },
  {
    key: 'workspace',
    title: '工作区',
    paths: ['/usage', '/enterprise', '/my-agents', '/agent-assets', '/remote-pentests'],
  },
  {
    key: 'community',
    title: '社区与支持',
    paths: ['/forum', '/support'],
  },
] as const

export type AdminNavigationGroupKey = typeof ADMIN_NAVIGATION_GROUPS[number]['key']
