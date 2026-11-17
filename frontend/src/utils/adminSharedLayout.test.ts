import { describe, expect, it } from 'vitest'

import { isAdminSharedRoute, shouldUseAdminSharedLayout } from './adminSharedLayout'

describe('administrator shared layout routes', () => {
  it.each([
    '/support',
    '/billing',
    '/forum',
    '/forum/',
    '/forum/new',
    '/forum/38',
    '/forum/38/edit',
  ])('uses the administrator shell for %s', (path) => {
    expect(isAdminSharedRoute(path)).toBe(true)
  })

  it.each([
    '/admin/operations',
    '/dashboard',
    '/forums',
    '/forumish',
  ])('does not claim an unrelated path: %s', (path) => {
    expect(isAdminSharedRoute(path)).toBe(false)
  })

  it.each(['/forum', '/forum/new', '/forum/38', '/forum/38/edit'])(
    'allows an administrator to use the shared shell on %s',
    (path) => {
      expect(shouldUseAdminSharedLayout(path, true)).toBe(true)
      expect(shouldUseAdminSharedLayout(path, false)).toBe(false)
    },
  )

  it('does not use the administrator shell for ordinary routes', () => {
    expect(shouldUseAdminSharedLayout('/admin/operations', true)).toBe(false)
    expect(shouldUseAdminSharedLayout('/dashboard', false)).toBe(false)
  })
})
