import { expect, it, vi } from 'vitest'
const api = vi.hoisted(() => ({ download: vi.fn() }))
vi.mock('@/api/http', () => ({ download: api.download }))
import { fetchAgentResponseImage } from './agentResponses'

it('历史图片显式要求重新鉴权，不能直接复用旧版本一小时缓存', async () => {
  api.download.mockResolvedValue(new Blob(['image'], { type: 'image/png' }))
  await fetchAgentResponseImage(12)
  expect(api.download).toHaveBeenCalledWith('/agent-responses/assets/12/image', undefined, {
    headers: { 'Cache-Control': 'no-cache', Pragma: 'no-cache' },
  })
})
