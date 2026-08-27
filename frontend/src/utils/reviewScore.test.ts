import { describe, expect, it } from 'vitest'

import { reviewRiskLevel, reviewScoreBucket, reviewScoreColor } from './reviewScore'

describe('review score contract', () => {
  it('uses the same four boundaries as backend reports', () => {
    expect([80, 60, 40, 0].map(reviewRiskLevel)).toEqual(['低风险', '中风险', '高风险', '极高风险'])
    expect([80, 60, 40, 0].map(reviewScoreBucket)).toEqual(['high', 'medium', 'risk', 'low'])
    expect([80, 60, 40, 0].map(reviewScoreColor)).toEqual([
      '#4FB87A',
      '#D9A857',
      '#E27C4A',
      '#DC4961',
    ])
  })

  it('keeps values immediately below each boundary in the higher-risk bucket', () => {
    expect([79.99, 59.99, 39.99].map(reviewRiskLevel)).toEqual(['中风险', '高风险', '极高风险'])
  })
})
