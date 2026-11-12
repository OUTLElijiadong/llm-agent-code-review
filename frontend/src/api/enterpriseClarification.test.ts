import { beforeEach, describe, expect, it, vi } from 'vitest'

const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('./http', () => ({ ...http, default: http }))

import {
  decideEnterpriseAssistantClarification,
  getEnterpriseAssistantClarification,
  resumeEnterpriseAssistantClarification,
} from './enterprise'

beforeEach(() => {
  http.get.mockReset()
  http.post.mockReset()
})

describe('企业小菱澄清 API', () => {
  it('读取当前运行的澄清预览并固定本次会话凭据', async () => {
    const preview = { request_id: 9, can_resume: false, candidates: [] }
    http.get.mockResolvedValue(preview)

    await expect(getEnterpriseAssistantClarification(10, 11, 'run-1', 'session-1')).resolves.toBe(preview)

    expect(http.get).toHaveBeenCalledWith(
      '/enterprises/10/conversations/11/assistant-runs/run-1/clarification',
      undefined,
      [40300, 40301, 40303, 40400, 40901],
      true,
      { authTokenSnapshot: 'session-1' },
    )
  })

  it('提交精确来源决定，继续使用同一会话凭据', async () => {
    const result = { request_id: 9, status: 'denied' }
    http.post.mockResolvedValue(result)

    await expect(decideEnterpriseAssistantClarification(10, 11, 'run-1', 9, 21, 'deny', 'session-1'))
      .resolves.toBe(result)

    expect(http.post).toHaveBeenCalledWith(
      '/enterprises/10/conversations/11/assistant-runs/run-1/clarification/9/decision',
      { source_message_id: 21, decision: 'deny' },
      undefined,
      [40300, 40301, 40303, 40400, 40901],
      true,
      { authTokenSnapshot: 'session-1' },
    )
  })

  it('走独立恢复路由而非普通运行重试', async () => {
    const run = { run_id: 'run-1', status: 'running' }
    http.post.mockResolvedValue(run)

    await expect(resumeEnterpriseAssistantClarification(10, 11, 'run-1', 9, 'session-1')).resolves.toBe(run)

    expect(http.post).toHaveBeenCalledWith(
      '/enterprises/10/conversations/11/assistant-runs/run-1/clarification/9/resume',
      {},
      undefined,
      [40200, 40300, 40301, 40303, 40360, 40361, 40400, 40901],
      true,
      { authTokenSnapshot: 'session-1' },
    )
  })
})
