import { get, put } from './http'

export interface EnterpriseProactivePolicy {
  enabled: boolean
  monthly_credits: number
  version: number
  metering_ready: boolean
  worker_ready: boolean
}

export const getPolicy = (enterpriseId: number, token: string | null) =>
  get<EnterpriseProactivePolicy>(`/enterprises/${enterpriseId}/conversations/proactive-settings`,
    undefined, [40300, 40301, 40303, 40400], true, { authTokenSnapshot: token })

export const updatePolicy = (enterpriseId: number, input: { enabled: boolean; monthly_credits: number; expected_version: number }, token: string | null) =>
  put<EnterpriseProactivePolicy>(`/enterprises/${enterpriseId}/conversations/proactive-settings`, input,
    { authTokenSnapshot: token })
