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
  sandboxStatusPresentation,
  sortSandboxEvents,
  sandboxEventMessage,
  stageLabel,
} from './sandboxPresentation'
import type { SandboxEnvironment } from '@/types/sandbox'

const baseEnvironment: SandboxEnvironment = {
  public_id: 'sbx_1', project_id: 1, owner_id: 1, agent_code: 'test_verifier',
  purpose: 'test', language: 'python', test_mode: 'whitebox', status: 'running',
  runtime: 'runsc', source_sha256: 'a'.repeat(64), expires_at: '2026-08-05T00:00:00',
  result: {}, events: [], artifacts: [],
}

describe('sandbox presentation rules', () => {
  it('labels current and legacy worker stages according to the actual test mode', () => {
    expect(stageLabel('running_whitebox', 'whitebox')).toBe('白盒测试')
    expect(stageLabel('running_whitebox', 'blackbox')).toBe('黑盒测试')
    expect(stageLabel('running_whitebox', 'combined')).toBe('黑白盒测试')
    expect(stageLabel('running_blackbox')).toBe('黑盒测试')
    expect(stageLabel('running_combined')).toBe('黑白盒测试')
  })

  it('projects only exact legacy terminal result messages according to the persisted test mode', () => {
    const failed = { event_type: 'result', stage: 'failed', message: '白盒测试失败' }
    const succeeded = { event_type: 'result', stage: 'succeeded', message: '白盒测试完成' }

    expect(sandboxEventMessage(failed, 'blackbox')).toBe('黑盒测试失败')
    expect(sandboxEventMessage(failed, 'combined')).toBe('黑白盒测试失败')
    expect(sandboxEventMessage(succeeded, 'blackbox')).toBe('黑盒测试完成')
    expect(sandboxEventMessage(succeeded, 'combined')).toBe('黑白盒测试完成')
    expect(sandboxEventMessage(failed, 'whitebox')).toBe('白盒测试失败')
    expect(sandboxEventMessage({ ...failed, event_type: 'failed' }, 'blackbox')).toBe('白盒测试失败')
    expect(sandboxEventMessage({ ...failed, stage: 'running' }, 'blackbox')).toBe('白盒测试失败')
    expect(sandboxEventMessage({ ...failed, message: '白盒测试失败：runner 超时' }, 'blackbox'))
      .toBe('白盒测试失败：runner 超时')
  })

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

  it('shows a partial verification as partial in task badges and the conclusion panel', () => {
    const partial = {
      ...baseEnvironment,
      status: 'succeeded',
      result: {
        passed: true,
        summary: '确定性白盒测试通过；AI动态补充未执行',
        evidence: { verification_coverage: { verification_status: 'partial' } },
      },
    }

    expect(sandboxStatusPresentation(partial)).toEqual({ label: '部分通过', type: 'warning' })
    expect(sandboxConclusionPresentation(partial)).toEqual({
      type: 'warning',
      title: '确定性白盒测试通过；AI动态补充未执行',
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
