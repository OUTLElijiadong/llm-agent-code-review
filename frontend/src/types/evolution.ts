/**
 * Agent 自进化模块类型定义,对齐后端 app/schemas/evolution.py
 */

/** 单个问题类型的反馈聚合 */
export interface FeedbackStat {
  issue_type: string
  rule_type: string
  fixed: number
  ignored: number
  decided: number
  acceptance_rate: number
  false_positive_rate: number
  distinct_tasks: number
  distinct_ignored_tasks: number
  distinct_ignored_users: number
}

/** 反馈信号总览 */
export interface FeedbackSummary {
  window_days: number
  total_fixed: number
  total_ignored: number
  total_decided: number
  overall_acceptance_rate: number
  overall_false_positive_rate: number
  by_issue_type: FeedbackStat[]
}

/** 进化提案 */
export interface EvolutionProposal {
  id: number
  proposal_type: string
  target_rule_id?: number | null
  title: string
  payload?: Record<string, unknown> | null
  evidence?: Record<string, unknown> | null
  status: string
  eval_score?: Record<string, unknown> | null
  applied_rule_id?: number | null
  created_by: string
  reviewed_by?: number | null
  reviewed_at?: string | null
  note?: string | null
  create_time?: string | null
}

/** 经验记忆条目 */
export interface ReviewExperience {
  id: number
  fingerprint: string
  language: string
  issue_type: string
  title?: string | null
  canonical_suggestion?: string | null
  accepted_count: number
  rejected_count: number
  weight: number
  last_seen?: string | null
  create_time?: string | null
}

/** 黄金集用例 */
export interface EvalCase {
  id: number
  name: string
  language: string
  expected_issues?: unknown
  tags?: string | null
  enabled: number
  source: string
}

/** 运行一轮进化的返回 */
export interface RunResult {
  harvest: { clusters: number; scanned: number }
  agent: {
    fp_proposals?: number
    new_rule_proposals?: number
    created?: number
    skipped?: number
    error?: string
  }
}
