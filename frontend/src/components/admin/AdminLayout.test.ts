import { describe, expect, it } from 'vitest'

import source from './AdminLayout.vue?raw'

describe('AdminLayout scrolling contract', () => {
  it('keeps sidebar and content in a fixed independent scroll shell', () => {
    expect(source).toContain('height: 100dvh')
    expect(source).toContain('overflow: hidden')
    expect(source).toContain('min-height: 0')
    expect(source).toContain('overflow-y: auto')
    expect(source).toContain('scrollTo')
    expect(source).toContain('overscroll-behavior: contain')
  })
})
