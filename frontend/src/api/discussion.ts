import { get } from './http'

export interface StartDiscussionParams {
  project_id: number
  file_id: number
  review_type: string
}

export interface StartDiscussionResult {
  session_id: string
  ws_url: string
  file_name: string
  language: string
  review_type: string
  agents: Array<{ code: string; name: string }>
  rules_count: number
}

export function startDiscussion(params: StartDiscussionParams) {
  return get<StartDiscussionResult>('/discuss/start', params)
}
