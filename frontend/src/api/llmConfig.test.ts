import { describe, expect, it, vi } from 'vitest'

const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn() }))
vi.mock('./http', () => http)

import { fetchLlmModels, testLlmConfig, updateLlmConfig } from './llmConfig'

describe('llm config API contract', () => {
  it('sends complete runtime options when updating', async () => {
    http.put.mockResolvedValueOnce({ timeout_seconds: 45 })
    await updateLlmConfig({
      provider: 'openai', base_url: 'https://api.example.com/v1', model: 'gpt-4o',
      api_key: 'sk-test', active: true, timeout_seconds: 45, max_retries: 4, temperature: 1.1,
    })
    expect(http.put).toHaveBeenCalledWith('/admin/llm/config', expect.objectContaining({ max_retries: 4 }))
  })

  it('exposes model discovery and preserves test metadata', async () => {
    http.post.mockResolvedValueOnce({ models: ['a'], selected_model: 'a' })
    await fetchLlmModels({ base_url: 'https://api.example.com/v1', model: 'a' })
    expect(http.post).toHaveBeenCalledWith('/admin/llm/models', expect.anything())

    http.post.mockResolvedValueOnce({ success: true, model: 'a', duration_ms: 12 })
    const result = await testLlmConfig({ model: 'a' })
    expect(result.duration_ms).toBe(12)
  })
})
