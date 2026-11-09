import { beforeEach, expect, it, vi } from 'vitest'
const http = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }))
vi.mock('./http', () => http)
import { getPolicy, updatePolicy } from './enterpriseProactive'

beforeEach(() => vi.clearAllMocks())

it('读取和修改主动讨论设置都绑定账号令牌快照与版本', async () => {
  await getPolicy(10, 'session-a')
  expect(http.get).toHaveBeenCalledWith('/enterprises/10/conversations/proactive-settings', undefined,
    [40300, 40301, 40303, 40400], true, { authTokenSnapshot: 'session-a' })
  await updatePolicy(10, { enabled: false, monthly_credits: 0, expected_version: 3 }, 'session-a')
  expect(http.put).toHaveBeenCalledWith('/enterprises/10/conversations/proactive-settings',
    { enabled: false, monthly_credits: 0, expected_version: 3 }, { authTokenSnapshot: 'session-a' })
})
