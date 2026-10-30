/**
 * 评分类型（分数的含义）判定：全站唯一口径。
 *
 * 生产圆桌任务 #236（62 分）在工作台显示“历史评分（类型未确认）”，在项目详情显示“代码评分”，
 * 原因是五个页面各自内联了一份 review_type 白名单，且白名单互不相同。现在所有页面只调用
 * scoreKindOf()：同一个任务无论在哪个页面，都得到同一个标签。
 *
 * 只决定分数的“类型”，不改变任何评分公式；评分版本（severity-deduction-v1、cvss-weighted-v1、
 * sandbox-check-pass-rate-v1、pentest-confirmed-deduction-v1、remote-config-observation-v1）由后端
 * ai/scoring.py 与各领域服务维护。判定只依赖所有页面都已拿到的 review_type 与 coverage，
 * 所以不需要给工作台、项目详情等接口新增字段。
 *
 * 与 reviewType.ts 的“审查类型”标签相互独立：那里回答“这是什么审查”，这里回答“这个分数代表什么”。
 */

export type ScoreKindKey =
  | 'code'
  | 'roundtable'
  | 'security_audit'
  | 'sandbox'
  | 'sandbox_partial'
  | 'pentest'
  | 'legacy'

export interface ScoreKind {
  key: ScoreKindKey
  /** 展示给用户的评分类型，例如“圆桌评分”。 */
  label: string
  /** 一句话说明分数依据，用于悬浮说明；不含具体数值。 */
  basis: string
}

export interface ScoreKindInput {
  review_type?: string | null
  /** 只读取 verification_status（沙箱验证是否完整）；其余键被忽略。 */
  coverage?: object | null
}

export const SCORE_KIND_LABELS: Record<ScoreKindKey, string> = {
  code: '代码评分',
  roundtable: '圆桌评分',
  security_audit: '安全审计评分',
  sandbox: '测试评分',
  sandbox_partial: '已测范围分',
  pentest: '测试评分',
  legacy: '历史评分（类型未确认）',
}

export const SCORE_KIND_BASIS: Record<ScoreKindKey, string> = {
  code: '按问题严重度扣分',
  roundtable: '按圆桌讨论抽取的问题集严重度计分，只代表被讨论的文件',
  security_audit: '按安全审计发现的严重度与 CVSS 向量计分',
  sandbox: '已执行检查项通过率',
  sandbox_partial: '已执行检查项通过率，验证范围不完整，只代表已测部分',
  pentest: '按已确认发现扣分',
  legacy: '评分来源类型未记录，仅作历史参考',
}

const CODE_REVIEW_TYPES = new Set(['quick', 'standard', 'security', 'performance', 'full'])
const ROUNDTABLE_TYPES = new Set(['discuss', 'repo_discuss'])
const SECURITY_AUDIT_TYPES = new Set(['security_scan', 'fullchain_audit'])

function kind(key: ScoreKindKey): ScoreKind {
  return { key, label: SCORE_KIND_LABELS[key], basis: SCORE_KIND_BASIS[key] }
}

/**
 * 判定任务分数的类型。缺失或未登记的 review_type 如实归为“类型未确认”，
 * 不猜测；新增审查类型时在这里登记一次，所有页面同时生效。
 */
export function scoreKindOf(task: ScoreKindInput | null | undefined): ScoreKind {
  const type = String(task?.review_type ?? '').trim()
  if (CODE_REVIEW_TYPES.has(type)) return kind('code')
  if (ROUNDTABLE_TYPES.has(type)) return kind('roundtable')
  if (SECURITY_AUDIT_TYPES.has(type)) return kind('security_audit')
  if (type === 'pentest') return kind('pentest')
  if (type === 'sandbox_test') {
    const verification = (task?.coverage as { verification_status?: unknown } | null | undefined)?.verification_status
    // 只有验证范围完整，沙箱分才代表整体测试结果；其余情况只代表已测范围。
    return kind(verification === 'complete' ? 'sandbox' : 'sandbox_partial')
  }
  return kind('legacy')
}
