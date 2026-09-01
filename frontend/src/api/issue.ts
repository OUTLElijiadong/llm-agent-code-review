import { get, put, post } from './http'
import type { Page } from '@/types/common'
import type {
  IssueOut,
  IssueListItemOut,
  IssueQuery,
  IssueUpdateStatusIn,
  IssueBatchUpdateStatusIn,
  IssueReviewDecisionIn,
} from '@/types/review'

/**
 * 跨任务/项目分页查询问题
 * @param params 查询条件
 * @returns 分页问题列表
 */
export function list(params: IssueQuery = {}): Promise<Page<IssueListItemOut>> {
  return get<Page<IssueListItemOut>>('/issues', params as Record<string, unknown>)
}

/**
 * 获取单个问题详情
 * @param id 问题 ID
 * @returns 问题详细信息
 */
export function getDetail(id: number): Promise<IssueOut> {
  return get<IssueOut>(`/issues/${id}`)
}

/**
 * 更新单个问题状态
 * @param id 问题 ID
 * @param body 新状态
 * @returns void
 */
export function updateStatus(id: number, body: IssueUpdateStatusIn): Promise<void> {
  return put<void>(`/issues/${id}/status`, body)
}

/**
 * 批量更新问题状态
 * @param body 问题 ID 列表及目标状态
 * @returns void
 */
export function batchUpdateStatus(body: IssueBatchUpdateStatusIn): Promise<void> {
  return post<void>('/issues/batch-status', body)
}

/** 对多智能体聚合争议做人工裁决。 */
export function reviewDecision(id: number, body: IssueReviewDecisionIn): Promise<IssueOut> {
  return put<IssueOut>(`/issues/${id}/review-decision`, body)
}
