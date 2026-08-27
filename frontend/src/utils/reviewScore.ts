/** 审查综合评分的唯一前端口径，与后端 score_risk_level 对齐。 */
export type ReviewScoreBucket = 'high' | 'medium' | 'risk' | 'low'

const REVIEW_SCORE_COLORS: Record<ReviewScoreBucket, string> = {
  high: '#4FB87A',
  medium: '#D9A857',
  risk: '#E27C4A',
  low: '#DC4961',
}

export function reviewRiskLevel(score: number | null | undefined): string {
  const value = Number(score ?? 0)
  if (value >= 80) return '低风险'
  if (value >= 60) return '中风险'
  if (value >= 40) return '高风险'
  return '极高风险'
}

export function reviewScoreBucket(score: number | null | undefined): ReviewScoreBucket {
  const value = Number(score ?? 0)
  if (value >= 80) return 'high'
  if (value >= 60) return 'medium'
  if (value >= 40) return 'risk'
  return 'low'
}

export function reviewScoreColor(score: number | null | undefined): string {
  return REVIEW_SCORE_COLORS[reviewScoreBucket(score)]
}
