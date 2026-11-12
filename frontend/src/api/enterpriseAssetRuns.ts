import { get, post } from './http'

export interface EnterpriseAssetRun {
  id: number
  enterprise_publication_id: number
  version_number: number
  status: 'running' | 'succeeded' | 'awaiting_reconciliation' | 'result_unavailable'
  output: string | null
  result_reason: string | null
  actual_milli: number | null
  created_at: string
}

const auth = (token: string | null) => ({ authTokenSnapshot: token })
export const runEnterpriseAsset = (id: number, input: { request_key: string; input_text: string }, token: string | null) =>
  post<EnterpriseAssetRun>(`/enterprise-asset-catalog/${id}/runs`, input, undefined, undefined, false, auth(token))
export const getEnterpriseAssetRun = (id: number, runId: number, token: string | null) =>
  get<EnterpriseAssetRun>(`/enterprise-asset-catalog/${id}/runs/${runId}`, undefined, undefined, false, auth(token))
export const getEnterpriseAssetRunByRequest = (id: number, key: string, token: string | null) =>
  get<EnterpriseAssetRun>(`/enterprise-asset-catalog/${id}/runs/by-request/${encodeURIComponent(key)}`, undefined, undefined, false, auth(token))
