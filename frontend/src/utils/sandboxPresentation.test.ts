import { describe, expect, it } from 'vitest'

import {
  canExtendSandbox,
  canStopSandbox,
  hasSandboxConclusion,
  isRemoteAuthorizationRequired,
  isSandboxActive,
  projectSandboxLanguage,
  sandboxConclusionPresentation,
  sandboxStatusLabel,
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
    expect(isSandboxActive('finalizing')).toBe(true)
    expect(canStopSandbox('finalizing')).toBe(true)
    expect(canExtendSandbox('finalizing')).toBe(true)
    expect(canExtendSandbox('stopping')).toBe(false)
    expect(sandboxStatusLabel('finalizing')).toBe('生成报告中')
    expect(canStopSandbox('stopping')).toBe(false)
    expect(hasSandboxConclusion(baseEnvironment)).toBe(false)
    expect(hasSandboxConclusion({ ...baseEnvironment, status: 'succeeded' })).toBe(true)
    expect(isRemoteAuthorizationRequired('combined', 'https://target.example')).toBe(true)
    expect(isRemoteAuthorizationRequired('whitebox', 'https://target.example')).toBe(false)
  })

  it('treats a finalizing result as pending report generation instead of a failure conclusion', () => {
    expect(sandboxConclusionPresentation({
      ...baseEnvironment,
      status: 'finalizing',
      result: { passed: true, summary: '白盒和黑盒测试已通过' },
    })).toEqual({
      type: 'warning',
      title: '确定性结果已生成，审查报告生成中',
    })
    expect(sandboxConclusionPresentation({
      ...baseEnvironment,
      status: 'succeeded',
      result: { passed: true, summary: '最终测试通过' },
    })).toEqual({
      type: 'success',
      title: '最终测试通过',
    })
    expect(sandboxConclusionPresentation({
      ...baseEnvironment,
      status: 'failed',
      result: { passed: false, summary: '最终测试失败' },
    })).toEqual({
      type: 'error',
      title: '最终测试失败',
    })
  })

  it('maps project languages to the fixed deployment runtime profiles', () => {
    expect(projectSandboxLanguage('PHP')).toBe('php')
    expect(projectSandboxLanguage('TypeScript')).toBe('node')
    expect(projectSandboxLanguage('node.js')).toBe('node')
    expect(projectSandboxLanguage('Golang')).toBe('go')
    expect(projectSandboxLanguage('PHP 8.3')).toBe('php')
    expect(projectSandboxLanguage('Python 3')).toBe('python')
    expect(projectSandboxLanguage('Node.js 20')).toBe('node')
    expect(projectSandboxLanguage('plaintext')).toBeNull()
  })
})
