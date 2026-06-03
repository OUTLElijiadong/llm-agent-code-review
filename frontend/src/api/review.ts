import { get, post, del } from './http'
import type { Page } from '@/types/common'
import type { ReviewStartIn, TaskOut, TaskDetailOut, IssueOut } from '@/types/review'

export function startReview(data: ReviewStartIn) {
  return post<{ task_id: number; status: string }>('/review/start', data)
}

export function getReviewTasks(params?: Record<string, unknown>) {
  return get<Page<TaskOut>>('/review/tasks', params)
}

export function getReviewTaskDetail(taskId: number) {
  return get<TaskDetailOut>(`/review/tasks/${taskId}`)
}

export function getTaskIssues(taskId: number, params?: Record<string, unknown>) {
  return get<Page<IssueOut>>(`/review/tasks/${taskId}/issues`, params)
}

export function deleteReviewTask(taskId: number) {
  return del<void>(`/review/tasks/${taskId}`)
}

export function cancelReviewTask(taskId: number) {
  return post<void>(`/review/tasks/${taskId}/cancel`)
}
