import { describe, expect, it } from 'vitest'

import router from './index'

describe('Agent Studio route permissions', () => {
  it('allows ordinary members with the private draft permission to enter Agent Studio', () => {
    const route = router.getRoutes().find((item) => item.name === 'AgentStudio')

    expect(route).toBeDefined()
    expect(route?.meta.roles).toContain('user')
    expect(route?.meta.permissions).toContain('agent_asset:create')
  })
})
