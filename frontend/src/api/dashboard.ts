import { get } from './http'
import type { SummaryOut, RiskItem, IssueTypeItem, ScoreTrendItem, FrequencyItem } from '@/types/dashboard'

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
