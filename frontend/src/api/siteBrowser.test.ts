import { beforeEach, describe, expect, it, vi } from 'vitest'
import { siteBrowserApi } from './siteBrowser'
import { get, post } from '@/api/http'

vi.mock('@/api/http', () => ({ get: vi.fn(), post: vi.fn() }))
describe('site browser authenticated protocol', () => {
  beforeEach(() => vi.clearAllMocks())
  it('uses the registered API paths and owner context for claim, ack and recovery', async () => {
    const context = { session_id: 'session-7', surface: 'user' as const, client_nonce: 'owner-nonce' }
    await siteBrowserApi.claim(7, context)
    expect(post).toHaveBeenCalledWith('/agent-browser/actions/7/claim', context, undefined, [40901], true)
    const receipt = { ...context, claim_token: 'one-claim', outcome: 'completed' as const,
      observation: { snapshot_id: 's', route: '/forum', title: '论坛', controls: [], messages: [] } }
    await siteBrowserApi.ack(7, receipt)
    expect(post).toHaveBeenCalledWith('/agent-browser/actions/7/ack', receipt, undefined, [40901], true)
    await siteBrowserApi.pending({ session_id: 'session-7', surface: 'user', run_id: 'run-7' })
    expect(get).toHaveBeenCalledWith('/agent-browser/pending', { session_id: 'session-7', surface: 'user', run_id: 'run-7' }, [40901], true)
  })
})
