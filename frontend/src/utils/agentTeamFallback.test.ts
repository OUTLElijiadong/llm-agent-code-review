import { describe, expect, it } from 'vitest'

import { agentTeamFallbackKey, buildAgentTeamFallback } from './agentTeamFallback'
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
})
