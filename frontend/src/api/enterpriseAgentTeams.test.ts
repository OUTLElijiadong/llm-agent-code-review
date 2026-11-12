import { beforeEach, describe, expect, it, vi } from 'vitest'

const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), head: vi.fn() }))
vi.mock('./http', () => ({ ...http, default: http }))

import {
  checkEnterpriseTeamSecurityArtifactAccess,
  getEnterpriseTeamSecurityArtifact,
  listEnterpriseAgentTeams,
} from './enterpriseAgentTeams'

beforeEach(() => {
  http.get.mockReset()
  http.post.mockReset()
  http.head.mockReset()
})

describe('企业小菱团队安全报告 API', () => {
  it('团队分页使用会话游标和原账号快照', async () => {
    http.get.mockResolvedValue({ items: [], has_more: false, next_before_id: null })
    await listEnterpriseAgentTeams(10, 9, 'session-token', 21)
    expect(http.get).toHaveBeenCalledWith(
      '/enterprises/10/agent-teams',
      { conversation_id: 9, limit: 20, before_id: 21 },
      [40300, 40301, 40303, 40400], true, { authTokenSnapshot: 'session-token' },
    )
  })
  it('使用企业、团队和制品范围，并绑定发起时账号快照', async () => {
    const artifact = { artifact_id: 91, enterprise_id: 10, team_id: 12, task_id: 33 }
    http.get.mockResolvedValue(artifact)

    await expect(getEnterpriseTeamSecurityArtifact(10, 12, 91, 'session-token')).resolves.toBe(artifact)

    expect(http.get).toHaveBeenCalledWith(
      '/enterprises/10/agent-teams/12/security-artifacts/91',
      undefined,
      [40300, 40301, 40303, 40400],
      true,
      { authTokenSnapshot: 'session-token' },
    )
  })

  it('以无正文 HEAD 请求重新校验当前团队和项目访问权', async () => {
    http.head.mockResolvedValue({ status: 204 })

    await expect(checkEnterpriseTeamSecurityArtifactAccess(10, 12, 91, 'session-token')).resolves.toBeUndefined()

    expect(http.head).toHaveBeenCalledWith(
      '/enterprises/10/agent-teams/12/security-artifacts/91',
      {
        silentCodes: [40300, 40301, 40303, 40400],
        silentStatuses: [403, 404],
        silentTransportError: true,
        authTokenSnapshot: 'session-token',
      },
    )
  })
})
