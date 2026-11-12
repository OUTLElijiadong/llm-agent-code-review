import { get, post } from './http'

export type PublicationStatus = 'pending' | 'approved' | 'rejected' | 'withdrawn' | 'revoked'
export interface PersonalPublication {
  id: number
  agent_id: number
  version_number: number
  title: string
  status: PublicationStatus
  decision_reason: string
  revocation_reason: string
  create_time: string
}
export interface PersonalPublicationReview extends PersonalPublication {
  prompt: string
  owner_id: number
  content_hash: string
}
export interface PublicAgent {
  id: number
  title: string
  version_number: number
  execution_available: boolean
}
export interface PublicAgentRun {
  id: number
  agent_id: null
  publication_id: number
  version_number: number
  status: 'running' | 'succeeded' | 'awaiting_reconciliation' | 'result_unavailable'
  output: string | null
  result_reason?: string | null
  actual_milli: number | null
  created_at: string
}

const auth = (token: string | null) => ({ authTokenSnapshot: token })

export const listPersonalPublications = (token: string | null, offset = 0) =>
  get<PersonalPublication[]>('/me/personal-agent-publications', { offset, limit: 100 }, undefined, false, auth(token))
export const submitPersonalPublication = (agentId: number, input: { expected_version: number; request_key: string }, token: string | null) =>
  post<PersonalPublication>(`/me/agents/${agentId}/publication-requests`, input, undefined, undefined, false, auth(token))
export const withdrawPersonalPublication = (id: number, token: string | null) =>
  post<PersonalPublication>(`/me/personal-agent-publications/${id}/withdraw`, {}, undefined, undefined, false, auth(token))
export const listPersonalPublicationReviews = (status: PublicationStatus, token: string | null, offset = 0) =>
  get<PersonalPublicationReview[]>('/admin/personal-agent-publications', { status, offset, limit: 100 }, undefined, false, auth(token))
export const decidePersonalPublication = (id: number, input: { approve: boolean; reason: string }, token: string | null) =>
  post<PersonalPublication>(`/admin/personal-agent-publications/${id}/decision`, input, undefined, undefined, false, auth(token))
export const revokePersonalPublication = (id: number, reason: string, token: string | null) =>
  post<PersonalPublication>(`/admin/personal-agent-publications/${id}/revoke`, { reason }, undefined, undefined, false, auth(token))
export const listPublicAgents = (token: string | null, offset = 0) =>
  get<PublicAgent[]>('/personal-agent-catalog', { offset, limit: 100 }, undefined, false, auth(token))
export const runPublicAgent = (id: number, input: { request_key: string; input_text: string }, token: string | null) =>
  post<PublicAgentRun>(`/personal-agent-catalog/${id}/runs`, input, undefined, undefined, false, auth(token))
export const getPublicAgentRun = (id: number, runId: number, token: string | null) =>
  get<PublicAgentRun>(`/personal-agent-catalog/${id}/runs/${runId}`, undefined, undefined, false, auth(token))
export const getPublicAgentRunByRequest = (id: number, key: string, token: string | null) =>
  get<PublicAgentRun>(`/personal-agent-catalog/${id}/runs/by-request/${encodeURIComponent(key)}`, undefined, undefined, false, auth(token))
