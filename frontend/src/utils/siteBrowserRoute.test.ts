import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it } from 'vitest'
import { expectedSiteRoute } from './siteBrowserRoute'

describe('expectedSiteRoute', () => {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/security', component: {} },
    { path: '/rules', redirect: { path: '/security', query: { section: 'rules' } } },
    { path: '/enterprise', component: {} },
    { path: '/loop', redirect: '/loop' },
  ] })
  it('resolves declared aliases and preserves query semantics', () => {
    expect(expectedSiteRoute(router, '/rules?view=all')).toBe('/security?view=all&section=rules')
    expect(expectedSiteRoute(router, '/enterprise?tab=forum')).toBe('/enterprise?tab=forum')
    expect(expectedSiteRoute(router, '/loop')).toBeNull()
    expect(expectedSiteRoute(router, '/missing')).toBeNull()
  })
})
