export type ResponseStreamEventType =
  | 'response.created'
  | 'response.output_text.delta'
  | 'response.output_item.added'
  | 'response.output_item.done'
  | 'response.function_call_arguments.delta'
  | 'response.function_call_arguments.done'
  | 'response.tool.started'
  | 'response.tool.completed'
  | 'response.tool.failed'
  | 'response.approval.required'
  | 'response.input.required'
  | 'response.sensitive.result'
  | 'response.completed'
  | 'response.incomplete'
  | 'response.failed'
  | 'response.cancelled'
  | 'auth_expired'
  | 'error'

export interface ResponseStreamEventBase {
  type: ResponseStreamEventType
  sequence_number?: number
  [key: string]: unknown
}

export interface ResponseCreatedEvent extends ResponseStreamEventBase {
  type: 'response.created'
  response: Record<string, unknown>
}

export interface ResponseOutputTextDeltaEvent extends ResponseStreamEventBase {
  type: 'response.output_text.delta'
  delta: string
  item_id?: string
  output_index?: number
  content_index?: number
}

export interface ResponseOutputItemAddedEvent extends ResponseStreamEventBase {
  type: 'response.output_item.added' | 'response.output_item.done'
  output_index?: number
  item: Record<string, unknown>
}

export interface ResponseToolLifecycleEvent extends ResponseStreamEventBase {
  type: 'response.tool.started' | 'response.tool.completed' | 'response.tool.failed'
  call_id?: string
  item_id?: string
  tool_name?: string
  name?: string
  agent_code?: string
  arguments?: string | Record<string, unknown>
  output?: unknown
  output_summary?: unknown
  preview?: unknown
  error?: string | Record<string, unknown>
  cached?: boolean
}

export interface ResponseInputOption {
  label: string
  value: string | number | boolean
  description?: string
}

export interface ResponseInputQuestion {
  key?: string
  question?: string
  label?: string
  title?: string
  options?: Array<ResponseInputOption | string | number | boolean | Record<string, unknown> | null>
  [key: string]: unknown
}

export interface ResponseFunctionCallArgumentsDeltaEvent extends ResponseStreamEventBase {
  type: 'response.function_call_arguments.delta'
  delta: string
  item_id?: string
  output_index?: number
}

export interface ResponseFunctionCallArgumentsDoneEvent extends ResponseStreamEventBase {
  type: 'response.function_call_arguments.done'
  arguments: string
  item_id?: string
  output_index?: number
}

export interface ResponseApprovalRequiredEvent extends ResponseStreamEventBase {
  type: 'response.approval.required'
  run_id: string
  call_id: string
  tool_name: string
  arguments: Record<string, unknown>
  operation: string
  impact: string
  danger: boolean
  preview?: unknown
}

export interface ResponseInputRequiredEvent extends ResponseStreamEventBase {
  type: 'response.input.required'
  run_id: string
  call_id?: string
  question?: string
  questions?: ResponseInputQuestion[]
  options?: Array<ResponseInputOption | string | number | boolean | Record<string, unknown> | null>
  arguments?: Record<string, unknown>
  allow_custom?: boolean
  allow_free_text?: boolean
}

export interface ResponseSensitiveResultEvent extends ResponseStreamEventBase {
  type: 'response.sensitive.result'
  run_id: string
  call_id: string
  capability: 'beta_codes.generate' | 'users.reset_password'
  title: string
  notice: string
  values: string[]
}

export interface ResponseApprovalDecision {
  action: 'approve' | 'reject'
  confirmation?: string
}

export interface ResponseTerminalEvent extends ResponseStreamEventBase {
  type: 'response.completed' | 'response.incomplete' | 'response.failed' | 'response.cancelled'
  response: Record<string, unknown>
}

export interface ResponseErrorEvent extends ResponseStreamEventBase {
  type: 'error'
  message?: string
  error?: {
    message?: string
    type?: string
    code?: string | null
    [key: string]: unknown
  }
}

export interface ResponseAuthExpiredEvent extends ResponseStreamEventBase {
  type: 'auth_expired'
  code: 40102
  message: string
}

export type ResponseStreamEvent =
  | ResponseCreatedEvent
  | ResponseOutputTextDeltaEvent
  | ResponseOutputItemAddedEvent
  | ResponseFunctionCallArgumentsDeltaEvent
  | ResponseFunctionCallArgumentsDoneEvent
  | ResponseToolLifecycleEvent
  | ResponseApprovalRequiredEvent
  | ResponseInputRequiredEvent
  | ResponseSensitiveResultEvent
  | ResponseTerminalEvent
  | ResponseAuthExpiredEvent
  | ResponseErrorEvent

export interface ResponsesStreamOptions {
  endpoint?: string
  signal?: AbortSignal
  onEvent: (event: ResponseStreamEvent) => void
  onError?: (error: unknown) => void
}

export interface ResponsesStreamHandle {
  abort: () => void
  signal: AbortSignal
  done: Promise<void>
}
