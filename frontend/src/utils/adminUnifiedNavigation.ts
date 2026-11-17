import type { LocationQueryRaw } from 'vue-router'

export type AdminCenterDomain = 'agents' | 'operations' | 'access' | 'platform'

/** 管理中心真实可选分区；导航确认与页面使用同一约定。 */
export const ADMIN_CENTER_SECTIONS: Record<AdminCenterDomain, readonly string[]> = {
  agents: ['agents', 'studio', 'approvals', 'evolution', 'knowledge', 'skills'],
  operations: ['overview', 'policies', 'tools', 'jobs', 'observability', 'logs', 'pentest-evidence', 'rewards', 'rollback'],
  access: ['users', 'roles', 'permissions'],
  platform: ['beta-codes', 'report-templates', 'llm', 'embedding', 'mcp-workers'],
}

/** Keep bookmarked legacy admin sections inside the current unified centers. */
export function resolveAdminSection(domain: AdminCenterDomain, requested: string): string {
  if (domain === 'agents' && requested === 'releases') return 'approvals'
  if (domain === 'operations' && ['ai-logs', 'audit'].includes(requested)) return 'logs'
  return requested
}

export function isKnownAdminSection(domain: AdminCenterDomain, requested: string): boolean {
  return ADMIN_CENTER_SECTIONS[domain].includes(resolveAdminSection(domain, requested))
}

export function sectionQuery(current: LocationQueryRaw, section: string): LocationQueryRaw {
  return { ...current, section }
}
