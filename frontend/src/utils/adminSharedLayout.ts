/** Shared pages rendered in the administrator shell for platform administrators. */
export function isAdminSharedRoute(path: string): boolean {
  const normalized = path.replace(/\/+$/, '').toLowerCase()
  return normalized === '/support'
    || normalized === '/billing'
    || normalized === '/forum'
    || normalized.startsWith('/forum/')
}

/** Use the administrator shell only when both the route and account allow it. */
export function shouldUseAdminSharedLayout(path: string, isAdmin: boolean): boolean {
  return isAdmin && isAdminSharedRoute(path)
}
