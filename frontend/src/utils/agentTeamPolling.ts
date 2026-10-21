import type { AgentTeamDetail, AgentTeamStatus, AgentTeamSummary } from '@/api/agentTeams'

const TERMINAL_STATUSES: ReadonlySet<AgentTeamStatus> = new Set([
  'completed', 'failed', 'blocked', 'cancelled', 'expired',
])

/** 仅复用已成功读取且账本版本未变的终态详情；未知/失败读取仍需重试。 */
export function shouldRefreshAgentTeamDetail(
  summary: AgentTeamSummary,
  cached: AgentTeamDetail | undefined,
  explicitlyViewed = false,
): boolean {
  return explicitlyViewed || !cached
    || !TERMINAL_STATUSES.has(cached.status)
    || !TERMINAL_STATUSES.has(summary.status)
    || summary.status !== cached.status
    || Boolean(summary.updated_at && summary.updated_at !== cached.updated_at)
}
