import type { SandboxEnvironment, SandboxEvent, SandboxLanguage } from '@/types/sandbox'

const ACTIVE_STATUSES = new Set(['queued', 'dispatching', 'running', 'ready', 'stopping'])
const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'blocked', 'stopped', 'expired'])
const PROJECT_LANGUAGE_TO_SANDBOX: Record<string, SandboxLanguage> = {
  python: 'python', py: 'python',
  javascript: 'node', js: 'node', typescript: 'node', ts: 'node',
  node: 'node', nodejs: 'node', 'node.js': 'node', vue: 'node', svelte: 'node',
  java: 'java', go: 'go', golang: 'go', php: 'php',
}
const PROJECT_LANGUAGE_COMPACT_ALIASES = Object.entries(PROJECT_LANGUAGE_TO_SANDBOX)
  .map(([alias, runtime]) => [alias.replace(/[^a-z0-9]+/g, ''), runtime] as const)
  .sort((left, right) => right[0].length - left[0].length)

export function projectSandboxLanguage(language?: string | null): SandboxLanguage | null {
  const normalized = (language || '').trim().toLowerCase().replace(/[_-]/g, '')
  const exact = PROJECT_LANGUAGE_TO_SANDBOX[normalized]
  if (exact) return exact
  const compact = normalized.replace(/[^a-z0-9]+/g, '')
  for (const [alias, runtime] of PROJECT_LANGUAGE_COMPACT_ALIASES) {
    const suffix = compact.slice(alias.length)
    if (compact === alias || (compact.startsWith(alias) && /^\d+$/.test(suffix))) return runtime
  }
  return null
}

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
