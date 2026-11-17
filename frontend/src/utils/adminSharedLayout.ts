/** Shared pages rendered in the administrator shell for platform administrators. */
export function isAdminSharedRoute(path: string): boolean {
  const normalized = path.replace(/\/+$/, '')
  return normalized === '/support' || normalized === '/billing'
}
