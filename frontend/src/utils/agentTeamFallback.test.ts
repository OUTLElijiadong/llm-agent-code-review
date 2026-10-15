import { describe, expect, it } from 'vitest'

import { agentTeamFallbackKey, buildAgentTeamFallback, terminalTeamReceiptsForFailedRun } from './agentTeamFallback'
import type { AgentTeamDetail } from '@/api/agentTeams'

function detail(overrides: Partial<AgentTeamDetail> = {}): AgentTeamDetail {
  return {
    team_id: 41,
    title: '提交代码的自动审查',
    surface: 'user',
    session_id: 'session-41',
    status: 'failed',
    max_active_children: 3,
    trace_id: 'trace-41',
    counts: { total: 2, completed: 1, running: 0, queued: 0, failed: 1, blocked: 0 },
    summary: { final_result: { summary: '审查子 Agent 已保存 1 项需复核问题。' } },
    members: [],
    events: [],
    messages: [],
    tasks: [
      {
        task_id: 1, task_key: 'review', member_id: 1, member_key: 'reviewer',
        title: '只读代码审查', depends_on: [], status: 'completed', priority: 0,
        attempt_count: 1, max_attempts: 3,
        result: {
          summary: '发现动态执行用户输入。',
          findings: [{
            severity: '高', title: '不可信输入进入 eval', description: '可触发任意表达式执行。',
            file_name: 'handler.py', line_number: 18, evidence_quote: 'eval(user_value)',
          }],
        },
      },
      {
        task_id: 2, task_key: 'verify', member_id: 2, member_key: 'verifier',
        title: '独立核验', depends_on: ['review'], status: 'failed', priority: 0,
        attempt_count: 3, max_attempts: 3, result: null,
        errors: [{ code: 'provider_unavailable', message: '核验未成功，不能视为通过' }],
      },
    ],
    ...overrides,
  }
}

describe('团队审查自动部分结果', () => {
  it('仅从同账号、同入口、同会话的终态账本构造有边界的结果', () => {
    const result = buildAgentTeamFallback(detail(), {
      teamId: 41, surface: 'user', sessionId: 'session-41', upstreamError: 'HTTP 402 billing quota',
    })

    expect(result).toContain('团队审查部分结果（自动整理）')
    expect(result).toContain('额度或计费拒绝')
    expect(result).toContain('未经小菱最终模型复核')
    expect(result).toContain('不可信输入进入 eval')
    expect(result).toContain('handler.py:18')
    expect(result).toContain('核验未成功，不能视为通过')
    expect(buildAgentTeamFallback(detail(), {
      teamId: 41, surface: 'admin', sessionId: 'session-41', upstreamError: '',
    })).toBeNull()
    expect(buildAgentTeamFallback(detail(), {
      teamId: 41, surface: 'user', sessionId: 'another-session', upstreamError: '',
    })).toBeNull()
  })

  it('非终态团队不产生貌似最终的答复，重复回执使用稳定幂等键', () => {
    expect(buildAgentTeamFallback(detail({ status: 'running' }), {
      teamId: 41, surface: 'user', sessionId: 'session-41', upstreamError: '',
    })).toBeNull()
    expect(agentTeamFallbackKey('message-1')).toBe(agentTeamFallbackKey('message-1'))
  })

  it('按失败 run 的团队创建事件或 Mesh 消息 ID 精确关联当前会话回执', () => {
    const terminal = {
      schema_version: '1.0' as const, message_id: 'team-terminal-41', idempotency_key: 'team-terminal-41',
      trace_id: 'trace-41', correlation_id: 'team:41', causation_id: '',
      sent_from: 'system:agent_team', send_to: 'session:user:session-41', message_type: 'task.error',
      priority: 'normal' as const, subject: 'team done', status: 'completed', payload: { team_id: 41 },
      context: { team_id: 41, team_terminal: true }, artifacts: [], errors: [],
      requires_ack: false, max_attempts: 1, attempt_count: 1, expires_at: '', create_time: '', update_time: '',
    }
    const base = {
      surface: 'user' as const, session_id: 'session-41',
      run: { run_id: 'run-41', status: 'failed', model: '', rounds: 1, error: 'HTTP 402', updated_at: '' },
      messages: [], pending: null, mesh_messages: [terminal],
    }

    expect(terminalTeamReceiptsForFailedRun({
      ...base,
      events: [{ type: 'response.tool.completed', tool_name: 'create_agent_team', output: { team_id: 41 } }],
    })).toEqual([terminal])
    expect(terminalTeamReceiptsForFailedRun({
      ...base,
      run: { ...base.run, mesh_message_id: 'team-terminal-41' },
    })).toEqual([terminal])
    expect(terminalTeamReceiptsForFailedRun({
      ...base,
      events: [{ type: 'response.tool.completed', tool_name: 'create_agent_team', output: { team_id: 42 } }],
      mesh_messages: [{ ...terminal, send_to: 'session:user:other-session' }],
    })).toEqual([])
    expect(terminalTeamReceiptsForFailedRun({
      ...base,
      run: { ...base.run, status: 'completed', mesh_message_id: '' },
      events: [{ type: 'response.tool.completed', tool_name: 'create_agent_team', output: { team_id: 41 } }],
    })).toEqual([])
  })
})
