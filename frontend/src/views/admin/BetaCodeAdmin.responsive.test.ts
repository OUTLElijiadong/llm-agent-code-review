import { describe, expect, it } from 'vitest'

describe('BetaCodeAdmin responsive layout', () => {
  it('allows grid panels to shrink inside the admin content area', async () => {
    const source = (await import('./BetaCodeAdmin.vue?raw')).default as string

    expect(source).toContain('grid-template-columns: minmax(0, 1fr)')
    expect(source).toMatch(/\.generator,\s*\.code-list\s*\{\s*min-width:\s*0;/)
  })
})
