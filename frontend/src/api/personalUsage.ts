import { get, put } from './http'
import type { TokenUsageTrend } from './adminOverview'

export interface PersonalTokenUsageTrend extends TokenUsageTrend {
  input_tokens: number
  output_tokens: number
  component_unknown_calls: number
}

export function getPersonalTokenUsage(days = 30, token?: string | null): Promise<PersonalTokenUsageTrend> {
  return get<PersonalTokenUsageTrend>('/me/token-usage', { days }, undefined, false,
    token === undefined ? undefined : { authTokenSnapshot: token })
}

export interface PersonalBillingUsageDay {
  date: string
  input_tokens: number
  output_tokens: number
  reported_calls: number
  unknown_calls: number
  pending_calls: number
  charged_milli: number
  enterprise_wallet_charged_milli: number
  personal_wallet_charged_milli: number
  server_tool_search_calls: number
  server_tool_fetch_calls: number
  server_tool_usage_reported_calls: number
  search_success_calls: number
  fetch_success_calls: number
  search_charged_milli: number
  fetch_charged_milli: number
  search_unknown_calls: number
  fetch_unknown_calls: number
  search_unknown_milli: number
  fetch_unknown_milli: number
  search_pending_calls: number
  search_pending_milli: number
  fetch_pending_calls: number
  fetch_pending_milli: number
  search_not_billable_calls: number
  fetch_not_billable_calls: number
}

export interface PersonalBillingUsageTrend {
  timezone: string
  items: PersonalBillingUsageDay[]
  scope_note: string
}

export function getPersonalBillingUsage(days = 30, token?: string | null): Promise<PersonalBillingUsageTrend> {
  return get<PersonalBillingUsageTrend>('/billing/usage', { days }, undefined, false,
    token === undefined ? undefined : { authTokenSnapshot: token })
}

export interface EnterpriseUsageDay {
  date: string
  input_tokens: number
  output_tokens: number
  reported_calls: number
  unknown_calls: number
  pending_calls: number
  charged_milli: number
  server_tool_search_calls: number
  server_tool_fetch_calls: number
  server_tool_usage_reported_calls: number
  search_success_calls: number
  fetch_success_calls: number
  search_charged_milli: number
  fetch_charged_milli: number
  search_unknown_calls: number
  fetch_unknown_calls: number
  search_unknown_milli: number
  fetch_unknown_milli: number
  search_pending_calls: number
  search_pending_milli: number
  fetch_pending_calls: number
  fetch_pending_milli: number
  search_not_billable_calls: number
  fetch_not_billable_calls: number
}
export interface EnterpriseUsageTrend {
  timezone: string
  items: EnterpriseUsageDay[]
  scope_note: string
}
export function getEnterpriseUsage(enterpriseId: number, days: number, groupId?: number, memberId?: number,
                                   token?: string | null) {
  return get<EnterpriseUsageTrend>(`/billing/enterprises/${enterpriseId}/usage`,
    { days, group_id: groupId, member_id: memberId }, [40300, 40301, 40303, 40400], false,
    token === undefined ? undefined : { authTokenSnapshot: token })
}

export interface EnterpriseMonthlyLimit {
  scope: 'enterprise' | 'group' | 'member'
  group_id: number | null
  member_id: number | null
  period_start: string
  period_end: string
  limit_milli: number
  used_milli: number
  reserved_milli: number
  remaining_milli: number
}

export interface EnterpriseMonthlyLimits {
  period: string
  timezone: 'Asia/Shanghai'
  items: EnterpriseMonthlyLimit[]
}

export function getEnterpriseLimits(enterpriseId: number, period: string, groupId?: number, token?: string | null) {
  return get<EnterpriseMonthlyLimits>(`/billing/enterprises/${enterpriseId}/limits`,
    { period, group_id: groupId }, [40300, 40301, 40303, 40400], false,
    token === undefined ? undefined : { authTokenSnapshot: token })
}

export function setEnterpriseLimit(enterpriseId: number, input: {
  period: string
  scope: EnterpriseMonthlyLimit['scope']
  limit_credits: number
  group_id?: number
  member_id?: number
}, token?: string | null) {
  return put<EnterpriseMonthlyLimit>(`/billing/enterprises/${enterpriseId}/limits`, input,
    token === undefined ? undefined : { authTokenSnapshot: token })
}
