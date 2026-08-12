import { beforeEach, describe, expect, it, vi } from 'vitest'

const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('./http', () => http)

import {
  archiveAgentTeam,
  cancelAgentTeam,
  createAgentTeam,
  getAgentTeam,
  listAgentTeamMessages,
  listAgentTeams,
  retryAgentTeam,
} from './agentTeams'

beforeEach(() => {
  http.get.mockReset()
  http.post.mockReset()
})

describe('agent teams api', () => {
  it('uses the session-scoped list and detail endpoints', async () => {
    http.get.mockResolvedValue({ items: [], total: 0 })
    await listAgentTeams({ surface: 'user', session_id: 'session-001', status: 'running', limit: 10 })
    await getAgentTeam(42)
    await listAgentTeamMessages(42, 501, 100)

    expect(http.get).toHaveBeenNthCalledWith(1, '/agent-teams', {
      surface: 'user', session_id: 'session-001', status: 'running', limit: 10,
    })
    expect(http.get).toHaveBeenNthCalledWith(2, '/agent-teams/42')
    expect(http.get).toHaveBeenNthCalledWith(3, '/agent-teams/42/messages', {
      before_id: 501, limit: 100,
    })
  })

  it('keeps the graph payload and mutation actions explicit', async () => {
    const input = {
      surface: 'user' as const,
      session_id: 'session-001',
      title: '发布前验证',
      objective: '并行执行测试',
      members: [{ member_key: 'reader', display_name: '读取 Agent', address: 'agent:project_analyzer' }],
      tasks: [{ task_key: 'read', member_key: 'reader', title: '读取项目', instructions: '读取项目' }],
    }
    http.post.mockResolvedValue({ team_id: 42, status: 'queued' })

    await createAgentTeam(input)
    await cancelAgentTeam(42, '用户取消')
    await retryAgentTeam(42, ['read'], { read: '刷新实时状态后改用路径 B' })
    await archiveAgentTeam(42, '已验收')

    expect(http.post).toHaveBeenNthCalledWith(1, '/agent-teams', input)
    expect(http.post).toHaveBeenNthCalledWith(2, '/agent-teams/42/cancel', { reason: '用户取消' })
    expect(http.post).toHaveBeenNthCalledWith(3, '/agent-teams/42/retry', {
      task_keys: ['read'], strategy_changes: { read: '刷新实时状态后改用路径 B' },
    })
    expect(http.post).toHaveBeenNthCalledWith(4, '/agent-teams/42/archive', { reason: '已验收' })
  })
})
