import { describe, expect, it } from 'vitest'

import {
  canStopSandbox,
  hasSandboxConclusion,
  isRemoteAuthorizationRequired,
  isSandboxActive,
  projectSandboxLanguage,
  sortSandboxEvents,
} from './sandboxPresentation'
import type { SandboxEnvironment } from '@/types/sandbox'

const baseEnvironment: SandboxEnvironment = {
  public_id: 'sbx_1', project_id: 1, owner_id: 1, agent_code: 'test_verifier',
  purpose: 'test', language: 'python', test_mode: 'whitebox', status: 'running',
  runtime: 'runsc', source_sha256: 'a'.repeat(64), expires_at: '2026-08-05T00:00:00',
  result: {}, events: [], artifacts: [],
}

describe('sandbox presentation rules', () => {
  it('sorts Agent events by durable sequence before timestamps', () => {
    const events = [
      { id: 3, event_type: 'complete', stage: 'conclusion', message: '结论', payload: {}, create_time: '2026-08-02T10:00:00' },
      { id: 1, event_type: 'dispatch', stage: 'worker', message: '调用', payload: {}, create_time: '2026-08-02T10:00:02' },
      { id: 2, event_type: 'progress', stage: 'execute', message: '执行', payload: {}, create_time: '2026-08-02T10:00:01' },
    ]
    expect(sortSandboxEvents(events).map((event) => event.id)).toEqual([1, 2, 3])
  })

  it('separates lifecycle actions, conclusions, and remote authorization', () => {
    expect(isSandboxActive('running')).toBe(true)
    expect(canStopSandbox('stopping')).toBe(false)
    expect(hasSandboxConclusion(baseEnvironment)).toBe(false)
    expect(hasSandboxConclusion({ ...baseEnvironment, status: 'succeeded' })).toBe(true)
    expect(isRemoteAuthorizationRequired('combined', 'https://target.example')).toBe(true)
    expect(isRemoteAuthorizationRequired('whitebox', 'https://target.example')).toBe(false)
  })

  it('maps project languages to the fixed deployment runtime profiles', () => {
    expect(projectSandboxLanguage('PHP')).toBe('php')
    expect(projectSandboxLanguage('TypeScript')).toBe('node')
    expect(projectSandboxLanguage('node.js')).toBe('node')
    expect(projectSandboxLanguage('Golang')).toBe('go')
    expect(projectSandboxLanguage('plaintext')).toBeNull()
  })
})
