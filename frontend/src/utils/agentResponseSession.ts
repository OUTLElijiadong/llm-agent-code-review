export const AGENT_RESPONSE_SESSION_POLL_INTERVAL_MS = 1000
export const AGENT_RESPONSE_SESSION_IDLE_POLL_INTERVAL_MS = 3000

const ACTIVE_STATUSES = new Set([
  'running',
  'approving',
  'rejecting',
  'answering',
])

const WAITING_STATUSES = new Set([
  'waiting_approval',
  'waiting_input',
])

function normalizeStatus(status: string | null | undefined): string {
  return status?.trim().toLowerCase() ?? ''
}

export function isAgentResponseSessionActive(status: string | null | undefined): boolean {
  return ACTIVE_STATUSES.has(normalizeStatus(status))
}

export function isAgentResponseSessionWaiting(status: string | null | undefined): boolean {
  return WAITING_STATUSES.has(normalizeStatus(status))
}

export function isAgentResponseSessionOccupied(status: string | null | undefined): boolean {
  return isAgentResponseSessionActive(status) || isAgentResponseSessionWaiting(status)
}

/** 活跃任务保持秒级反馈,其余状态仍低频同步跨会话消息和最终状态。 */
export function agentResponseSessionPollInterval(status: string | null | undefined): number {
  return isAgentResponseSessionActive(status)
    ? AGENT_RESPONSE_SESSION_POLL_INTERVAL_MS
    : AGENT_RESPONSE_SESSION_IDLE_POLL_INTERVAL_MS
}
