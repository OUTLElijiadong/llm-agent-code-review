/** Page location is an untrusted hint; the server maps it to registered pages. */
export function currentPageContext(path: string, routeSection: string): { path: string; section: string } | undefined {
  if (path.length > 300 || !/^\/(?!\/)[a-zA-Z0-9/_-]*$/.test(path) || path.includes('..')) return undefined
  let section = routeSection
  if (['/enterprise', '/support', '/admin/governance', '/admin/operations', '/admin/access', '/admin/platform'].includes(path)) {
    // A route change can briefly leave an outgoing view mounted. Never guess when
    // the view is missing or multiple mounted views claim the same route.
    const pages = typeof document === 'undefined' ? [] : Array.from(
      document.querySelectorAll<HTMLElement>('[data-xiaoling-page]'),
    ).filter((element) => element.dataset.xiaolingPage === path)
    section = pages.length === 1 ? pages[0].dataset.xiaolingSection || '' : ''
  }
  if (section.length > 80 || !/^[a-zA-Z0-9_-]*$/.test(section)) section = ''
  return { path, section }
}
