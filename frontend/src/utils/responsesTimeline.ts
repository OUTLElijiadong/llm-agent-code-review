import type { ResponseInputRequiredEvent, ResponseStreamEvent } from '@/types/responses'

export type ResponseToolCallStatus =
  | 'streaming'
  | 'running'
  | 'waiting_approval'
  | 'waiting_input'
  | 'completed'
  | 'failed'
  | 'rejected'

export interface ResponseToolCall {
  key: string
  itemId?: string
  callId?: string
  outputIndex?: number
  name: string
  agentCode?: string
  argumentsText: string
  status: ResponseToolCallStatus
  resultPreview?: string
  error?: string
}

export interface NormalizedResponseInputOption {
  label: string
  value: string
  description?: string
}

const SENSITIVE_KEY = /(?:password|passwd|secret|token|api[_-]?key|authorization|credential|private[_-]?key|reasoning(?:[_-]?content)?|encrypted[_-]?content)/i
const SENSITIVE_TEXT_ASSIGNMENT = /((?:["']?(?:password|passwd|secret|token|api[_-]?key|authorization|credential|private[_-]?key|reasoning(?:[_-]?content)?|encrypted[_-]?content)["']?)\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|Bearer\s+[^\s,;\]}&]+|[^\s,;\]}&]+)/gi
const BEARER_SECRET = /\bBearer\s+[A-Za-z0-9._~+/=-]+/gi
const API_KEY_SECRET = /\bsk-[A-Za-z0-9_-]{8,}\b/gi
const PRIVATE_KEY_BLOCK = /-----BEGIN [^-\r\n]*PRIVATE KEY-----[\s\S]*?-----END [^-\r\n]*PRIVATE KEY-----/gi

function redactSensitiveText(value: string): string {
  const trimmed = value.trim()
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    try {
      const parsed = JSON.parse(trimmed) as unknown
      if (parsed && typeof parsed === 'object') return JSON.stringify(redactSensitive(parsed))
    } catch {
      // Partial function-call JSON is handled by the textual patterns below.
    }
  }
  return value
    .replace(PRIVATE_KEY_BLOCK, '[已隐藏私钥]')
    .replace(SENSITIVE_TEXT_ASSIGNMENT, '$1[已隐藏]')
    .replace(BEARER_SECRET, 'Bearer [已隐藏]')
    .replace(API_KEY_SECRET, '[已隐藏 API Key]')
}

function redactSensitive(value: unknown, key = ''): unknown {
  if (key && SENSITIVE_KEY.test(key)) return '[已隐藏]'
  if (typeof value === 'string') return redactSensitiveText(value)
  if (Array.isArray(value)) return value.map((item) => redactSensitive(item))
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .map(([childKey, child]) => [childKey, redactSensitive(child, childKey)]),
    )
  }
  return value
}

export function formatResponseValue(value: unknown): string {
  if (value === undefined || value === null || value === '') return ''
  if (typeof value === 'string') {
    const trimmed = value.trim()
    if (!trimmed) return ''
    try {
      return JSON.stringify(redactSensitive(JSON.parse(trimmed)), null, 2)
    } catch {
      return redactSensitiveText(trimmed)
    }
  }
  try {
    return JSON.stringify(redactSensitive(value), null, 2)
  } catch {
    return String(value)
  }
}

function textValue(value: unknown): string {
  if (value === undefined || value === null) return ''
  return formatResponseValue(value)
}

function toolCallFromItem(
  item: Record<string, unknown>,
  outputIndex?: number,
): ResponseToolCall | null {
  const itemType = String(item.type ?? '')
  if (itemType && itemType !== 'function_call' && !itemType.endsWith('_call')) return null
  const itemId = typeof item.id === 'string' ? item.id : undefined
  const callId = typeof item.call_id === 'string' ? item.call_id : undefined
  const name = String(item.name ?? item.tool_name ?? item.server_label ?? '工具调用')
  const status = item.status === 'failed' ? 'failed' : 'streaming'
  return {
    key: itemId ?? callId ?? `output-${outputIndex ?? crypto.randomUUID()}`,
    itemId,
    callId,
    outputIndex,
    name,
    argumentsText: formatResponseValue(item.arguments),
    status,
    error: status === 'failed' ? textValue(item.error) : undefined,
  }
}

function findCall(
  calls: ResponseToolCall[],
  identity: { itemId?: string; callId?: string; outputIndex?: number; name?: string },
): ResponseToolCall | undefined {
  return calls.find((call) => Boolean(identity.itemId && call.itemId === identity.itemId))
    ?? calls.find((call) => Boolean(identity.callId && call.callId === identity.callId))
    ?? calls.find((call) => identity.outputIndex !== undefined && call.outputIndex === identity.outputIndex)
    ?? [...calls].reverse().find((call) => Boolean(identity.name && call.name === identity.name
      && !['completed', 'failed', 'rejected'].includes(call.status)))
}

function ensureCall(
  calls: ResponseToolCall[],
  identity: { itemId?: string; callId?: string; outputIndex?: number; name?: string },
): ResponseToolCall {
  const existing = findCall(calls, identity)
  if (existing) return existing
  const call: ResponseToolCall = {
    key: identity.itemId ?? identity.callId ?? `tool-${crypto.randomUUID()}`,
    itemId: identity.itemId,
    callId: identity.callId,
    outputIndex: identity.outputIndex,
    name: identity.name || '工具调用',
    argumentsText: '',
    status: 'streaming',
  }
  calls.push(call)
  return call
}

export function isResponseToolEvent(event: ResponseStreamEvent): boolean {
  return event.type === 'response.output_item.added'
    || event.type === 'response.output_item.done'
    || event.type === 'response.function_call_arguments.delta'
    || event.type === 'response.function_call_arguments.done'
    || event.type === 'response.tool.started'
    || event.type === 'response.tool.completed'
    || event.type === 'response.tool.failed'
}

export function applyResponseToolEvent(
  calls: ResponseToolCall[],
  event: ResponseStreamEvent,
): void {
  if (event.type === 'response.output_item.added' || event.type === 'response.output_item.done') {
    const incoming = toolCallFromItem(event.item, event.output_index)
    if (!incoming) return
    const call = findCall(calls, incoming)
    if (!call) {
      calls.push(incoming)
      return
    }
    call.itemId ||= incoming.itemId
    call.callId ||= incoming.callId
    call.name = incoming.name || call.name
    if (incoming.argumentsText) call.argumentsText = incoming.argumentsText
    if (incoming.status === 'failed') {
      call.status = 'failed'
      call.error = incoming.error
    }
    return
  }

  if (event.type === 'response.function_call_arguments.delta') {
    const call = ensureCall(calls, {
      itemId: event.item_id,
      outputIndex: event.output_index,
    })
    // 分片 JSON 在完整闭合前无法可靠识别敏感键，不将原始分片放入可见时间线。
    call.status = 'streaming'
    return
  }

  if (event.type === 'response.function_call_arguments.done') {
    const call = ensureCall(calls, {
      itemId: event.item_id,
      outputIndex: event.output_index,
    })
    call.argumentsText = formatResponseValue(event.arguments) || call.argumentsText
    call.status = 'running'
    return
  }

  if (event.type === 'response.tool.started'
    || event.type === 'response.tool.completed'
    || event.type === 'response.tool.failed') {
    const call = ensureCall(calls, {
      itemId: event.item_id,
      callId: event.call_id,
      name: event.tool_name ?? event.name,
    })
    call.callId ||= event.call_id
    call.name = event.tool_name ?? event.name ?? call.name
    call.agentCode = event.agent_code ?? call.agentCode
    if (event.arguments !== undefined) call.argumentsText = formatResponseValue(event.arguments)
    if (event.type === 'response.tool.started') call.status = 'running'
    if (event.type === 'response.tool.completed') {
      call.status = 'completed'
      call.resultPreview = formatResponseValue(event.preview ?? event.output_summary ?? event.output)
    }
    if (event.type === 'response.tool.failed') {
      call.status = 'failed'
      call.error = formatResponseValue(event.error) || '工具执行失败'
    }
  }
}

export function attachApprovalToToolCall(
  calls: ResponseToolCall[],
  callId: string,
  toolName: string,
  argumentsValue: Record<string, unknown>,
): ResponseToolCall {
  const call = ensureCall(calls, { callId, name: toolName })
  call.callId ||= callId
  call.name = toolName || call.name
  call.argumentsText = formatResponseValue(argumentsValue) || call.argumentsText
  call.status = 'waiting_approval'
  return call
}

export function attachInputToToolCall(
  calls: ResponseToolCall[],
  event: ResponseInputRequiredEvent,
): ResponseToolCall {
  const call = ensureCall(calls, { callId: event.call_id, name: 'ask_user' })
  call.callId ||= event.call_id
  call.name = call.name === '工具调用' ? 'ask_user' : call.name
  call.argumentsText ||= formatResponseValue(event.arguments)
  call.status = 'waiting_input'
  return call
}

export function setResponseToolCallStatus(
  calls: ResponseToolCall[],
  callId: string | undefined,
  status: ResponseToolCallStatus,
  error?: string,
): boolean {
  if (!callId) return false
  const call = findCall(calls, { callId })
  if (!call) return false
  call.status = status
  call.error = error ? formatResponseValue(error) : undefined
  return true
}

export function finishResponseToolCalls(
  calls: ResponseToolCall[],
  status: 'failed',
  error?: string,
): void {
  for (const call of calls) {
    if (['completed', 'failed', 'rejected', 'waiting_approval', 'waiting_input'].includes(call.status)) continue
    call.status = status
    call.error = error ? formatResponseValue(error) : '调用未完成'
  }
}

function optionFromUnknown(value: unknown): NormalizedResponseInputOption | null {
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return { label: String(value), value: String(value) }
  }
  if (!value || typeof value !== 'object') return null
  const item = value as Record<string, unknown>
  const rawValue = item.value ?? item.id ?? item.code ?? item.name ?? item.label ?? item.title
  const rawLabel = item.label ?? item.title ?? item.name ?? item.text ?? rawValue
  if (rawValue === undefined || rawLabel === undefined) return null
  return {
    label: String(rawLabel),
    value: String(rawValue),
    description: typeof item.description === 'string'
      ? item.description
      : typeof item.hint === 'string' ? item.hint : undefined,
  }
}

export function responseInputQuestion(event: ResponseInputRequiredEvent): string {
  if (event.question?.trim()) return event.question.trim()
  const argumentQuestion = event.arguments?.question
  if (typeof argumentQuestion === 'string' && argumentQuestion.trim()) return argumentQuestion.trim()
  const first = event.questions?.[0]
  const value = first?.question ?? first?.label ?? first?.title
  return typeof value === 'string' && value.trim() ? value.trim() : '请补充信息后继续执行'
}

export function responseInputOptions(
  event: ResponseInputRequiredEvent,
): NormalizedResponseInputOption[] {
  const argumentOptions = event.arguments?.options
  const source = event.options
    ?? (Array.isArray(argumentOptions) ? argumentOptions : undefined)
    ?? event.questions?.find((question) => Array.isArray(question.options))?.options
    ?? []
  const seen = new Set<string>()
  const output: NormalizedResponseInputOption[] = []
  for (const item of source) {
    const option = optionFromUnknown(item)
    if (!option || seen.has(option.value)) continue
    seen.add(option.value)
    output.push(option)
  }
  return output
}

export function responseAllowsFreeText(event: ResponseInputRequiredEvent): boolean {
  if (typeof event.allow_free_text === 'boolean') return event.allow_free_text
  if (typeof event.allow_custom === 'boolean') return event.allow_custom
  const argumentValue = event.arguments?.allow_free_text
  return typeof argumentValue === 'boolean' ? argumentValue : true
}
