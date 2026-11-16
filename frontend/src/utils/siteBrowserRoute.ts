import type { Router } from 'vue-router'

/** Resolve only router-declared redirects, never a guard redirect to another page. */
export function expectedSiteRoute(router: Router, route: string): string | null {
  try {
    let current = router.resolve(route)
    const seen = new Set<string>()
    for (let depth = 0; depth < 8; depth++) {
      if (!current.matched.length || seen.has(current.fullPath)) return null
      seen.add(current.fullPath)
      const redirect = current.matched.at(-1)?.redirect
      if (!redirect) return current.fullPath
      if (typeof redirect === 'function') return null
      current = typeof redirect === 'string'
        ? router.resolve(redirect)
        : router.resolve({ ...redirect, query: { ...current.query, ...redirect.query } })
    }
  } catch { /* A malformed or unresolvable route cannot be verified. */ }
  return null
}
