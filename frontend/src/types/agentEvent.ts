export type AgentEventType =
  | 'dispatch' | 'thinking' | 'progress' | 'complete' | 'failed' | 'clarify'
  | 'admin_alert'

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
  type: 'text' | 'textarea' | 'select_project' | 'select_task' | 'code' | 'select' | 'number' | 'confirm' | 'danger_confirm'
  hint?: string
  required?: boolean
  options?: { value: string | number; label: string }[]
  /** v3.1: 后端模糊命中项目时预填的默认值,前端预选供用户一键确认 */
  default?: string | number
}

export interface ClarifyPayload {
  clarify_id: string
  intent: string
  questions: ClarifyQuestion[]
}
