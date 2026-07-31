export const AGENT_RESPONSE_SESSION_POLL_INTERVAL_MS = 1000

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
