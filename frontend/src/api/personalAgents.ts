import { get, post, put } from './http'

export interface PersonalAgent {
  id: number
  title: string
  prompt: string
  current_version: number
  status: 'active' | 'disabled'
  execution_available: boolean
  publication_available: boolean
  create_time: string
  update_time: string
}

export interface PersonalAgentRun {
  id: number
  agent_id: number
  version_number: number
  status: 'running' | 'succeeded' | 'awaiting_reconciliation' | 'result_unavailable'
  output: string | null
  result_reason?: string | null
  actual_milli: number | null
  created_at: string
}

type AgentInput = { title: string; prompt: string; request_key: string }
const auth = (token: string | null) => ({ authTokenSnapshot: token })

export function listPersonalAgents(token: string | null, offset = 0) {
  return get<PersonalAgent[]>('/me/agents', { offset, limit: 50 }, undefined, false, auth(token))
}
export function getPersonalAgent(id: number, token: string | null) {
  return get<PersonalAgent>(`/me/agents/${id}`, undefined, undefined, false, auth(token))
}
export function createPersonalAgent(input: AgentInput, token: string | null) {
  return post<PersonalAgent>('/me/agents', input, undefined, undefined, false, auth(token))
}
export function updatePersonalAgent(id: number, input: AgentInput & { expected_version: number }, token: string | null) {
  return put<PersonalAgent>(`/me/agents/${id}`, input, auth(token))
}
export function disablePersonalAgent(id: number, input: { expected_version: number }, token: string | null) {
  return post<PersonalAgent>(`/me/agents/${id}/disable`, input, undefined, undefined, false, auth(token))
}

export function createPersonalAgentRun(id: number, input: {
  request_key: string; version_number: number; input_text: string
}, token: string | null) {
  return post<PersonalAgentRun>(`/me/agents/${id}/runs`, input, undefined, undefined, false, auth(token))
}

export function getPersonalAgentRun(id: number, runId: number, token: string | null) {
  return get<PersonalAgentRun>(`/me/agents/${id}/runs/${runId}`, undefined, undefined, false, auth(token))
}

export function getPersonalAgentRunByRequest(id: number, requestKey: string, token: string | null) {
  return get<PersonalAgentRun>(`/me/agents/${id}/runs/by-request/${encodeURIComponent(requestKey)}`,
    undefined, undefined, false, auth(token))
}
