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

/**
 * v3.0 触发指定 Agent 的自进化 Skill(admin only)
 *
 * 调用 POST /api/evolution/trigger,通过 Orchestrator.invoke_skill
 * 调用 {agent_name}.self_improve Skill(trigger_type=manual),
 * 自动写 agent_skill_record 与 audit_log。
 *
 * 与 runEvolution 的区别:
 * - runEvolution: 调用旧版 EvolutionAgent.run()(全局进化,不写 skill 记录)
 * - triggerEvolution: 调用 per-Agent 的 self_improve Skill(写 skill 记录,可追溯)
 *
 * @param agentName - Agent name(如 code_reviewer / evolution),默认 evolution
 * @param windowDays - 反馈窗口天数(1-365),默认 90
 * @returns 触发结果 dict,含 success/data/effect/duration_ms/record_id
 */
export function triggerEvolution(
  agentName = 'evolution',
  windowDays = 90,
): Promise<Record<string, unknown>> {
  return post<Record<string, unknown>>(
    '/evolution/trigger',
    undefined,
    { agent_name: agentName, window_days: windowDays },
  )
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
