import { expect, it, vi } from 'vitest'

const http = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() }))
vi.mock('./http', () => http)
import { getEnterpriseAssistantBillingContext, requestEnterpriseAssistant, retryEnterpriseAssistantRun } from './enterprise'

it('计费预览使用当前会话路径和认证快照', async () => {
  const controller = new AbortController()
  await getEnterpriseAssistantBillingContext(10, 9, 'session-a', controller.signal)
  expect(http.get).toHaveBeenCalledWith('/enterprises/10/conversations/9/assistant-billing-context',
    undefined, [40300, 40301, 40303, 40400], true,
    { authTokenSnapshot: 'session-a', timeout: 15000, signal: controller.signal })
})

it('小菱归属不可用时交由会话界面展示，不误触发通用权限提示', async () => {
  await requestEnterpriseAssistant(10, 9, 7, 'session-a')
  expect(http.post).toHaveBeenLastCalledWith('/enterprises/10/conversations/9/assistant-runs',
    { source_message_id: 7 }, undefined, expect.arrayContaining([40300, 40360, 40361]), true,
    { authTokenSnapshot: 'session-a' })
  await retryEnterpriseAssistantRun(10, 9, 'run-1', 'session-a')
  expect(http.post).toHaveBeenLastCalledWith('/enterprises/10/conversations/9/assistant-runs/run-1/retry',
    {}, undefined, expect.arrayContaining([40300, 40360, 40361]), true, { authTokenSnapshot: 'session-a' })
})
