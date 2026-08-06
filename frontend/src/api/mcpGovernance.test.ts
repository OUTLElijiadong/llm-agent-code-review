import { beforeEach, describe, expect, it, vi } from 'vitest'

const http = vi.hoisted(() => ({ del: vi.fn(), get: vi.fn(), post: vi.fn(), put: vi.fn() }))
vi.mock('./http', () => http)

import {
  checkSandboxWorker,
  createSandboxWorker,
  listMcpServers,
  listSandboxWorkers,
  seedProductionFallbackWorker,
  updateSandboxWorker,
} from './mcpGovernance'

beforeEach(() => {
  Object.values(http).forEach((mock) => mock.mockReset())
})

describe('MCP and worker governance api', () => {
  it('keeps MCP governance under /admin/mcp', async () => {
    http.get.mockResolvedValue([])
    await listMcpServers()
    expect(http.get).toHaveBeenCalledWith('/admin/mcp/servers')
  })

  it('uses the final super-admin worker endpoints under /sandboxes/workers', async () => {
    const payload = {
      code: 'worker-1',
      name: 'Worker 1',
      worker_type: 'managed' as const,
      transport: 'https' as const,
      endpoint: 'https://worker.example',
      supported_languages: ['python' as const],
      supported_modes: ['whitebox' as const],
      runtime: 'runsc',
      max_concurrency: 1,
      priority: 50,
      enabled: true,
    }
    http.get.mockResolvedValue([])
    http.post.mockResolvedValue({ id: 1 })
    http.put.mockResolvedValue({ id: 1 })

    await listSandboxWorkers()
    await createSandboxWorker(payload)
    await updateSandboxWorker(1, payload)
    await checkSandboxWorker(1)
    await seedProductionFallbackWorker()

    expect(http.get).toHaveBeenCalledWith('/sandboxes/workers')
    expect(http.post).toHaveBeenNthCalledWith(1, '/sandboxes/workers', payload)
    expect(http.put).toHaveBeenCalledWith('/sandboxes/workers/1', payload)
    expect(http.post).toHaveBeenNthCalledWith(2, '/sandboxes/workers/1/health')
    expect(http.post).toHaveBeenNthCalledWith(3, '/sandboxes/workers/seed-production')
  })
})
