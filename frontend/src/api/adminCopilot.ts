export type AdminCopilotMessageType =
  | 'text'
  | 'confirm'
  | 'danger_confirm'
  | 'report'
  | 'alert'
  | 'table'

export interface AdminCopilotMessage {
  type: AdminCopilotMessageType
  title?: string
  content?: string
  operation?: string
  impact?: string
  consequence?: string
  action_token?: string
  status?: string
  summary?: string
  counts?: Record<string, number>
  count_labels?: Record<string, string>
  risks?: string[]
  suggestions?: string[]
  severity?: string
  description?: string
  suggestion?: string
  action_label?: string
  action_prompt?: string
  columns?: string[]
  rows?: Array<Record<string, unknown>>
  total?: number
  collapsed?: boolean
  message_id?: number
  user_message_id?: number
}
