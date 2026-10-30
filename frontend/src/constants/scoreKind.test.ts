import { describe, expect, it } from 'vitest'

import { SCORE_KIND_BASIS, SCORE_KIND_LABELS, scoreKindOf, type ScoreKindKey } from './scoreKind'

describe('评分类型判定（全站唯一口径）', () => {
  it.each(['quick', 'standard', 'security', 'performance', 'full'])('%s 是代码评分', (review_type) => {
    expect(scoreKindOf({ review_type }).key).toBe('code')
    expect(scoreKindOf({ review_type }).label).toBe('代码评分')
  })

  it('圆桌任务 #236（分数 62）有明确的中文评分类型，不落到“类型未确认”', () => {
    const kind = scoreKindOf({ review_type: 'discuss', coverage: { verification_status: 'complete' } })

    expect(kind.key).toBe('roundtable')
    expect(kind.label).toBe('圆桌评分')
    expect(kind.label).not.toContain('类型未确认')
    expect(kind.basis).toContain('圆桌')
  })

  it('全仓库圆桌同属圆桌评分', () => {
    expect(scoreKindOf({ review_type: 'repo_discuss' }).label).toBe('圆桌评分')
  })

  it.each(['security_scan', 'fullchain_audit'])('%s 是安全审计评分，不再落到类型未确认', (review_type) => {
    expect(scoreKindOf({ review_type }).key).toBe('security_audit')
    expect(scoreKindOf({ review_type }).label).toBe('安全审计评分')
  })

  it('沙箱验证完整时是测试评分，否则只代表已测范围', () => {
    expect(scoreKindOf({ review_type: 'sandbox_test', coverage: { verification_status: 'complete' } }).label)
      .toBe('测试评分')
    for (const status of ['partial', 'failed', 'unknown', null, undefined]) {
      const kind = scoreKindOf({ review_type: 'sandbox_test', coverage: { verification_status: status } })
      expect(kind.key).toBe('sandbox_partial')
      expect(kind.label).toBe('已测范围分')
    }
    expect(scoreKindOf({ review_type: 'sandbox_test' }).label).toBe('已测范围分')
    expect(scoreKindOf({ review_type: 'sandbox_test', coverage: null }).label).toBe('已测范围分')
  })

  it('渗透测试评分不受 verification_status 影响', () => {
    expect(scoreKindOf({ review_type: 'pentest' }).label).toBe('测试评分')
    expect(scoreKindOf({ review_type: 'pentest', coverage: { verification_status: 'partial' } }).label)
      .toBe('测试评分')
  })

  it.each([undefined, null, '', '   ', 'brand_new_type'])('缺失或未登记的类型 %j 如实标为类型未确认', (review_type) => {
    const kind = scoreKindOf({ review_type })

    expect(kind.key).toBe('legacy')
    expect(kind.label).toBe('历史评分（类型未确认）')
  })

  it('没有任何输入也不抛错', () => {
    expect(scoreKindOf(null).key).toBe('legacy')
    expect(scoreKindOf(undefined).key).toBe('legacy')
  })

  it('同一任务、不同页面传入同样的字段，必然得到同一个标签', () => {
    const task = { review_type: 'discuss', coverage: { stage: 'complete', coverage_status: 'complete' } }

    const labels = [
      scoreKindOf(task).label,
      scoreKindOf({ review_type: task.review_type }).label,
      scoreKindOf({ review_type: task.review_type, coverage: null }).label,
      scoreKindOf({ ...task, coverage: { ...task.coverage, extra: 1 } as never }).label,
    ]

    expect(new Set(labels).size).toBe(1)
  })

  it('每个评分类型都有中文标签与计分依据，且不含英文枚举', () => {
    const keys = Object.keys(SCORE_KIND_LABELS) as ScoreKindKey[]

    expect(keys.sort()).toEqual(
      ['code', 'legacy', 'pentest', 'roundtable', 'sandbox', 'sandbox_partial', 'security_audit'],
    )
    for (const key of keys) {
      expect(SCORE_KIND_LABELS[key]).toMatch(/[一-龥]/)
      expect(SCORE_KIND_LABELS[key]).not.toMatch(/[A-Za-z]/)
      expect(SCORE_KIND_BASIS[key]).toMatch(/[一-龥]/)
    }
  })
})
