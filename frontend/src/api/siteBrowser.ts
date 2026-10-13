import { get, post } from '@/api/http'
import type { ResponseBrowserRequiredEvent } from '@/types/responses'

export type SiteBrowserAction = 'inspect' | 'navigate' | 'click' | 'fill' | 'select' | 'check' | 'scroll'
export type SiteBrowserRequiredEvent = ResponseBrowserRequiredEvent
export interface SiteBrowserObservation {
  snapshot_id: string
  route: string
  title: string
  controls: Array<{ client_handle: string; role: 'button' | 'link' | 'textbox' | 'combobox' | 'checkbox' | 'radio' | 'tab' | 'option'; label: string; type: string; readonly: boolean }>
  messages: string[]
}
export interface SiteBrowserAck {
  session_id: string
  surface: 'user' | 'admin'
  client_nonce: string
  claim_token: string
  outcome: 'completed' | 'failed' | 'indeterminate' | 'user_required'
  observation: SiteBrowserObservation
  error?: string
}
export interface SiteBrowserApi {
  claim: (id: number, body: Pick<SiteBrowserAck, 'session_id' | 'surface' | 'client_nonce'>) => Promise<{ action_id: number; claim_token: string }>
  ack: (id: number, body: SiteBrowserAck) => Promise<unknown>
  pending: (query: { session_id: string; surface: 'user' | 'admin'; run_id: string }) => Promise<{ actions: Array<SiteBrowserRequiredEvent | { action_id: number; status: 'executing' }> }>
}
const base = '/agent-browser'
export const siteBrowserApi: SiteBrowserApi = {
  claim: (id, body) => post(`${base}/actions/${id}/claim`, body, undefined, [40901], true),
  ack: (id, body) => post(`${base}/actions/${id}/ack`, body, undefined, [40901], true),
  pending: query => get(`${base}/pending`, query, [40901], true),
}
