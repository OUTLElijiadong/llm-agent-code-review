import { get } from './http'
import type { RunningOut, SummaryOut, RiskItem, IssueTypeItem, ScoreTrendItem, FrequencyItem } from '@/types/dashboard'

export function getSummary(scope?: string) {
  return get<SummaryOut>('/dashboard/summary', { scope })
}

export function getRiskDistribution(days?: number) {
  return get<RiskItem[]>('/dashboard/risk-distribution', { days })
}

export function getIssueTypeStatistics(days?: number) {
  return get<IssueTypeItem[]>('/dashboard/issue-type-statistics', { days })
}

export function getScoreTrend(limit?: number) {
  return get<ScoreTrendItem[]>('/dashboard/score-trend', { limit })
}

export function getReviewFrequency(days?: number) {
  return get<FrequencyItem[]>('/dashboard/review-frequency', { days })
}

/** 后台进行中:排队/运行中的审查与本人进行中的 Agent 运行 */
export function getRunning() {
  return get<RunningOut>('/dashboard/running')
}
