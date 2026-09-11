import { describe, expect, it } from 'vitest'

describe('BetaCodeAdmin responsive layout', () => {
  it('allows grid panels to shrink inside the admin content area', async () => {
    const source = (await import('./BetaCodeAdmin.vue?raw')).default as string

    expect(source).toContain('grid-template-columns: minmax(0, 1fr)')
    expect(source).toMatch(/\.generator,\s*\.code-list\s*\{\s*min-width:\s*0;/)
  })

  it('renders the code list as summary cards instead of a table', async () => {
    const source = (await import('./BetaCodeAdmin.vue?raw')).default as string

    expect(source).not.toContain('<el-table')
    expect(source).toContain('class="code-cards"')
    expect(source).toContain('data-testid="code-cards"')
    expect(source).toContain('data-status')
    expect(source).toContain('prefers-reduced-motion')
    expect(source).toContain('@media (max-width: 760px)')
  })
})
