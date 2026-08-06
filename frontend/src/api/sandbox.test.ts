import { beforeEach, describe, expect, it, vi } from 'vitest'

const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('./http', () => http)

import {
  createSandbox,
  createSandboxPreviewSession,
  extendSandbox,
  getSandbox,
  listSandboxes,
  searchSandboxCapabilities,
  stopSandbox,
} from './sandbox'

beforeEach(() => {
  http.get.mockReset()
  http.post.mockReset()
})

describe('sandbox api', () => {
  it('uses the user sandbox lifecycle endpoints and preserves explicit authorization', async () => {
    http.get.mockResolvedValue([])
    http.post.mockResolvedValue({ public_id: 'sbx_1' })

    await listSandboxes(20)
    await getSandbox('sbx/a')
    await createSandbox({
      project_id: 7,
      purpose: 'test',
      language: 'php',
      test_mode: 'combined',
      ttl_hours: 72,
      remote_target_url: 'https://target.example',
      remote_target_authorized: true,
    })
    await extendSandbox('sbx/a', 24)
    await stopSandbox('sbx/a')

    expect(http.get).toHaveBeenNthCalledWith(1, '/sandboxes', { limit: 20 })
    expect(http.get).toHaveBeenNthCalledWith(2, '/sandboxes/sbx%2Fa')
    expect(http.post).toHaveBeenNthCalledWith(1, '/sandboxes', expect.objectContaining({
      project_id: 7,
      test_mode: 'combined',
      remote_target_authorized: true,
    }))
    expect(http.post).toHaveBeenNthCalledWith(2, '/sandboxes/sbx%2Fa/extend', { hours: 24 })
    expect(http.post).toHaveBeenNthCalledWith(3, '/sandboxes/sbx%2Fa/stop')
  })

  it('creates a preview session before opening the deployment path and searches capabilities', async () => {
    http.post.mockResolvedValue({ path: '/api/sandboxes/sbx_1/preview/' })
    http.get.mockResolvedValue([])

    await createSandboxPreviewSession('sbx_1')
    await searchSandboxCapabilities('整包扫描', 6)

    expect(http.post).toHaveBeenCalledWith('/sandboxes/sbx_1/preview-session')
    expect(http.get).toHaveBeenCalledWith('/sandboxes/capabilities/search', { q: '整包扫描', limit: 6 })
  })
})
