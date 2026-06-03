import { get, post, put, del } from './http'
import type { RuleOut } from '@/types/rule'

export function getRules() {
  return get<RuleOut[]>('/rules')
}

export function toggleRule(ruleId: number, enabled: number) {
  return post<null>(`/rules/${ruleId}/toggle`, { enabled })
}

export function createRule(data: Record<string, unknown>) {
  return post<{ id: number }>('/rules', data)
}

export function updateRule(ruleId: number, data: Record<string, unknown>) {
  return put<null>(`/rules/${ruleId}`, data)
}

export function deleteRule(ruleId: number) {
  return del<null>(`/rules/${ruleId}`)
}
