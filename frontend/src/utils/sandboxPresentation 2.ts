import type { SandboxEnvironment, SandboxEvent } from '@/types/sandbox'

const ACTIVE_STATUSES = new Set(['queued', 'dispatching', 'running', 'ready', 'stopping'])
const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'blocked', 'stopped', 'expired'])

export function sortSandboxEvents(events: SandboxEvent[]): SandboxEvent[] {
  return [...events].sort((left, right) => {
    if (left.id !== right.id) return left.id - right.id
    return new Date(left.create_time).getTime() - new Date(right.create_time).getTime()
  })
}

export function isSandboxActive(status: string): boolean {
  return ACTIVE_STATUSES.has(status)
}

export function canStopSandbox(status: string): boolean {
  return ACTIVE_STATUSES.has(status) && status !== 'stopping'
}

export function hasSandboxConclusion(environment: SandboxEnvironment): boolean {
  return TERMINAL_STATUSES.has(environment.status) || Object.keys(environment.result || {}).length > 0
}

export function isRemoteAuthorizationRequired(testMode: string, remoteUrl: string): boolean {
  return Boolean(remoteUrl.trim()) && (testMode === 'blackbox' || testMode === 'combined')
}
