export type AgentEventType =
  | 'dispatch' | 'thinking' | 'progress' | 'complete' | 'failed' | 'clarify'

export interface AgentEvent {
  type: AgentEventType
  agent: string
  trace_id: string
  parent?: string
  message: string
  payload: Record<string, unknown>
  timestamp: string
}

export interface ClarifyQuestion {
  key: string
  label: string
  type: 'text' | 'textarea' | 'select_project' | 'select_task' | 'code' | 'select' | 'number'
  hint?: string
  required?: boolean
  options?: { value: string | number; label: string }[]
}

export interface ClarifyPayload {
  clarify_id: string
  intent: string
  questions: ClarifyQuestion[]
}
