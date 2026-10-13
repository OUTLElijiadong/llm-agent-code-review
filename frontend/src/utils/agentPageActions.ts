import type { AgentTeamDetail } from '@/api/agentTeams'
import type { ResponseStreamEvent } from '@/types/responses'
import { isSafeXiaolingRoute } from '@/utils/xiaolingNavigation'
import { formatResponseValue, type ResponseToolCall } from '@/utils/responsesTimeline'

export const AGENT_PAGE_INVALIDATED_EVENT = 'prism:agent-page-invalidated'

/** 目标由服务端固定能力注册表与执行回执给出，模型参数不能指定它。 */
export interface AgentPageAction {
  page: string
  capability: string
  phase: 'submitted' | 'pending' | 'started' | 'completed' | 'failed'
  mutation: boolean
  result_route?: string
  account_id?: number
}

export function readAgentPageAction(value: unknown): AgentPageAction | null {
  if (!value || typeof value !== 'object') return null
  const item = value as Record<string, unknown>
  if (typeof item.page !== 'string' || !isSafeXiaolingRoute(item.page)
    || typeof item.capability !== 'string' || !item.capability.trim()
    || !['submitted', 'pending', 'started', 'completed', 'failed'].includes(String(item.phase))
    || typeof item.mutation !== 'boolean') return null
  const resultRoute = typeof item.result_route === 'string' && isSafeXiaolingRoute(item.result_route)
    ? item.result_route : undefined
  return { page: item.page, capability: item.capability, phase: item.phase as AgentPageAction['phase'],
    mutation: item.mutation, result_route: resultRoute,
    account_id: typeof item.account_id === 'number' ? item.account_id : undefined }
}

export function pageActionTargetHint(event: ResponseStreamEvent): string | undefined {
  const action = readAgentPageAction(event.page_action)
  return action?.result_route || action?.page
}

const delivered = new Set<string>()
function notify(action: AgentPageAction, key: string, accountId?: number): boolean {
  if (action.phase !== 'completed' || !action.mutation
    || (action.account_id !== undefined && action.account_id !== accountId)
    || delivered.has(key)) return false
  delivered.add(key)
  if (delivered.size > 500) delivered.delete(delivered.values().next().value!)
  window.dispatchEvent(new CustomEvent(AGENT_PAGE_INVALIDATED_EVENT, { detail: action }))
  return true
}

/** 仅真实完成回执可使页面失效；提交、排队和 worker 执行均不是验证通过。 */
export function notifyCompletedPageAction(event: ResponseStreamEvent, accountId?: number): boolean {
  if (event.type !== 'response.tool.completed') return false
  const action = readAgentPageAction(event.page_action)
  if (!action) return false
  return notify(action, `${accountId}:${event.run_id}:${event.call_id}:${action.capability}`, accountId)
}

export function notifyVerifiedTeamPageActions(team: AgentTeamDetail, accountId?: number): void {
  if (team.status !== 'completed' || (team.user_id !== undefined && team.user_id !== accountId)) return
  const verifiers = new Set(team.members.filter(member => member.role === 'verifier').map(member => member.member_id))
  for (const task of team.tasks) {
    if (task.status !== 'completed' || !verifiers.has(task.member_id) || !task.result) continue
    const result = task.result
    const receipts = Array.isArray(result.result_receipts)
      ? result.result_receipts : [result.result_receipt]
    for (const receipt of receipts) {
      if (!receipt || typeof receipt !== 'object') continue
      const action = readAgentPageAction((receipt as Record<string, unknown>).page_action)
      if (action) notify(action, `${accountId}:team:${team.team_id}:${task.task_id}:${action.capability}`, accountId)
    }
  }
}

/** 对齐工具时间线与真实团队终态，防止“已提交”长期停留或冒充“做好了”。 */
export function applyTeamResultToToolCalls(calls: ResponseToolCall[], team: AgentTeamDetail): void {
  for (const call of calls) {
    if (call.teamId !== team.team_id) continue
    if (team.status === 'failed' || team.status === 'expired') {
      call.status = 'failed'
      call.error = formatResponseValue(team.error) || '团队执行或结果核验未通过'
    } else if (team.status === 'cancelled') {
      call.status = 'rejected'
    } else if (team.status === 'completed') {
      const verifierIds = new Set(team.members.filter(member => member.role === 'verifier').map(member => member.member_id))
      const receipts = team.tasks.filter(task => task.status === 'completed' && verifierIds.has(task.member_id))
        .flatMap(task => Array.isArray(task.result?.result_receipts) ? task.result.result_receipts : [task.result?.result_receipt])
        .filter(receipt => receipt && typeof receipt === 'object'
          && readAgentPageAction((receipt as Record<string, unknown>).page_action)?.phase === 'completed')
      if (receipts.length) {
        call.status = 'completed'
        call.resultPreview = formatResponseValue(receipts)
      }
    }
  }
}
