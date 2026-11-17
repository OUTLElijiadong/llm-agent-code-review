/** Shared pages rendered in the administrator shell for platform administrators. */
export function isAdminSharedRoute(path: string): boolean {
  return path === '/support' || path === '/billing'
}
