import { get, post } from './http'
import type {
  EvalCase,
  EvolutionProposal,
  FeedbackSummary,
  ReviewExperience,
  RunResult,
} from '@/types/evolution'

/** 反馈信号总览 */
export function getFeedback(windowDays = 90) {
  return get<FeedbackSummary>('/evolution/feedback', { window_days: windowDays })
}

/** 经验记忆库(按权重降序) */
export function listExperiences(limit = 50) {
  return get<ReviewExperience[]>('/evolution/experiences', { limit })
}

/** 黄金回归集 */
export function listEvalCases() {
  return get<EvalCase[]>('/evolution/eval-cases')
}

/** 触发一轮进化:沉淀经验 + 生成候选提案 */
export function runEvolution(windowDays = 90) {
  return post<RunResult>('/evolution/run', { window_days: windowDays })
}

/** 进化提案列表(可按状态过滤) */
export function listProposals(status = '') {
  return get<EvolutionProposal[]>('/evolution/proposals', status ? { status } : undefined)
}

/** 进化提案详情 */
export function getProposal(id: number) {
  return get<EvolutionProposal>(`/evolution/proposals/${id}`)
}

/** 跑评估闸门 */
export function evaluateProposal(id: number) {
  return post<EvolutionProposal>(`/evolution/proposals/${id}/evaluate`)
}

/** 审批生效(需先过闸门) */
export function approveProposal(id: number) {
  return post<EvolutionProposal>(`/evolution/proposals/${id}/approve`)
}

/** 驳回提案 */
export function rejectProposal(id: number, note: string) {
  return post<EvolutionProposal>(`/evolution/proposals/${id}/reject`, { note })
}

/** 回滚已生效提案 */
export function rollbackProposal(id: number, note: string) {
  return post<EvolutionProposal>(`/evolution/proposals/${id}/rollback`, { note })
}
