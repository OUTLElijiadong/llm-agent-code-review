import type { LocationQueryRaw } from 'vue-router'

export type AdminCenterDomain = 'agents' | 'operations' | 'access' | 'platform'

/** Keep bookmarked legacy admin sections inside the current unified centers. */
export function resolveAdminSection(domain: AdminCenterDomain, requested: string): string {
  if (domain === 'agents' && ['releases', 'evolution'].includes(requested)) return 'approvals'
  if (domain === 'operations' && ['ai-logs', 'audit'].includes(requested)) return 'logs'
  return requested
}

export function sectionQuery(current: LocationQueryRaw, section: string): LocationQueryRaw {
  return { ...current, section }
}
