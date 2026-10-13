import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AgentTeamDetail } from '@/api/agentTeams'
import { AGENT_PAGE_INVALIDATED_EVENT, notifyCompletedPageAction, notifyVerifiedTeamPageActions,
  pageActionTargetHint, readAgentPageAction } from './agentPageActions'
import { applyTeamResultToToolCalls } from './agentPageActions'
import type { ResponseToolCall } from './responsesTimeline'

const pageAction = (overrides = {}) => ({ page: '/forum', capability: 'forum.posts.create',
  phase: 'completed', mutation: true, account_id: 7, result_route: '/forum/44', ...overrides })
afterEach(() => vi.restoreAllMocks())

describe('authorized page execution receipts', () => {
  it('takes a target from service metadata rather than model page arguments', () => {
    expect(pageActionTargetHint({ type: 'response.tool.started', arguments: { page: '/admin/access' },
      page_action: pageAction() })).toBe('/forum/44')
    expect(pageActionTargetHint({ type: 'response.tool.started', arguments: { page: '/admin/access' } })).toBeUndefined()
  })
  it.each(['https://evil.example/a', '//evil.example/a', 'javascript:alert(1)'])('rejects external target %s', page => {
    expect(readAgentPageAction(pageAction({ page }))).toBeNull()
  })
  it('ignores an unsafe result URL while retaining the fixed page', () => {
    expect(pageActionTargetHint({ type: 'response.tool.started', page_action: pageAction({ result_route: '//evil.example' }) }))
      .toBe('/forum')
  })
  it.each(['submitted', 'pending', 'failed'])('does not invalidate %s submissions', phase => {
    const emitted = vi.spyOn(window, 'dispatchEvent')
    expect(notifyCompletedPageAction({ type: 'response.tool.completed', call_id: `phase-${phase}`,
      page_action: pageAction({ phase }) }, 7)).toBe(false)
    expect(emitted).not.toHaveBeenCalled()
  })
  it('does not invalidate reads or another account, and deduplicates a real write receipt', () => {
    const emitted = vi.spyOn(window, 'dispatchEvent')
    expect(notifyCompletedPageAction({ type: 'response.tool.completed', call_id: 'read',
      page_action: pageAction({ mutation: false }) }, 7)).toBe(false)
    expect(notifyCompletedPageAction({ type: 'response.tool.completed', call_id: 'account-other',
      page_action: pageAction() }, 8)).toBe(false)
    const event = { type: 'response.tool.completed' as const, call_id: 'confirmed-write', page_action: pageAction() }
    expect(notifyCompletedPageAction(event, 7)).toBe(true)
    expect(notifyCompletedPageAction(event, 7)).toBe(false)
    expect(emitted).toHaveBeenCalledTimes(1)
    expect((emitted.mock.calls[0][0] as CustomEvent).type).toBe(AGENT_PAGE_INVALIDATED_EVENT)
  })
  it('waits for the independent verifier rather than the worker result', () => {
    const emitted = vi.spyOn(window, 'dispatchEvent')
    const team = { team_id: 909, user_id: 7, status: 'running',
      members: [{ member_id: 1, role: 'worker' }, { member_id: 2, role: 'verifier' }],
      tasks: [{ task_id: 1, member_id: 1, status: 'completed', result: { result_receipt: { page_action: pageAction() } } },
        { task_id: 2, member_id: 2, status: 'running', result: null }],
    } as unknown as AgentTeamDetail
    notifyVerifiedTeamPageActions(team, 7)
    expect(emitted).not.toHaveBeenCalled()
    team.status = 'completed'
    notifyVerifiedTeamPageActions(team, 7)
    expect(emitted).not.toHaveBeenCalled()
    team.tasks[1].status = 'completed'
    team.tasks[1].result = { result_receipt: { page_action: pageAction() } }
    notifyVerifiedTeamPageActions(team, 8)
    expect(emitted).not.toHaveBeenCalled()
    notifyVerifiedTeamPageActions(team, 7)
    notifyVerifiedTeamPageActions(team, 7)
    expect(emitted).toHaveBeenCalledTimes(1)
  })
  it('supports verifier receipts for multiple independent page writes', () => {
    const emitted = vi.spyOn(window, 'dispatchEvent')
    const team = { team_id: 910, user_id: 7, status: 'completed',
      members: [{ member_id: 2, role: 'verifier' }], tasks: [{ task_id: 2, member_id: 2, status: 'completed',
        result: { result_receipts: [{ page_action: pageAction() },
          { page_action: pageAction({ page: '/support', capability: 'feedback.create' }) }] } }],
    } as unknown as AgentTeamDetail
    notifyVerifiedTeamPageActions(team, 7)
    expect(emitted).toHaveBeenCalledTimes(2)
  })
  it('only finishes a submitted step when the verifier has a completed receipt; failures remain failures', () => {
    const calls: ResponseToolCall[] = [{ key: 'queued', name: 'user_execute_capability', teamId: 77,
      status: 'submitted', argumentsText: '' }]
    const team = { team_id: 77, status: 'completed', members: [{ member_id: 2, role: 'verifier' }],
      tasks: [{ member_id: 2, status: 'completed', result: { result_receipt: { page_action: pageAction({ phase: 'failed' }) } } }],
    } as unknown as AgentTeamDetail
    applyTeamResultToToolCalls(calls, team)
    expect(calls[0].status).toBe('submitted')
    team.tasks[0].result = { result_receipt: { page_action: pageAction() } }
    applyTeamResultToToolCalls(calls, team)
    expect(calls[0].status).toBe('completed')
    expect(calls[0].resultPreview).toContain('forum.posts.create')
    team.status = 'failed'; team.error = { message: '结果未落库' }
    applyTeamResultToToolCalls(calls, team)
    expect(calls[0].status).toBe('failed')
    expect(calls[0].error).toContain('结果未落库')
  })
})
