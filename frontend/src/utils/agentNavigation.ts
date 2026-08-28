/**
 * Prism Agent 页面引导协议
 *
 * 1. 导航卡片:助手回复里以 ``[标题](/路由)`` 形式出现的站内链接,
 *    点击后通过 router 解析目标并在路由守卫放行时 SPA 内跳转;
 *    无权或外部链接一律不跳转,从界面上隐藏,避免模型编造路由造成越权入口。
 * 2. 指令导航:回复末尾的 ``<!--PRISM_NAVIGATE {...}-->`` 注释是模型
 *    与系统约定的"带我去"协议,解析出合法路由后展示为导航按钮并自动跳转。
 */
import type { RouteMeta, Router, RouteRecordRaw } from 'vue-router'
import type { AgentNavigateDirective } from '@/types/agentGuide'
import { normalizeAgentText, transformOutsideCodeFences } from '@/utils/agentText'
import { renderMarkdown } from '@/utils/markdown'

const NAVIGATE_DIRECTIVE_RE = /<!--\s*PRISM_NAVIGATE\s*([\s\S]*?)\s*-->/g
const MAX_DIRECTIVE_JSON_LENGTH = 2048
const KNOWN_ROUTE_RE = /\/[a-z0-9][a-z0-9/_-]*(?:\?[a-z0-9%&=_.~-]+)?(?:#[a-z0-9%_.~-]+)?/gi
const NAVIGATION_CUE_RE = /路由|页面|导航|前往|进入|打开|带(?:我|你)去/

function flattenRoutes(routes: readonly RouteRecordRaw[], prefix: string): RouteRecordRaw[] {
  const out: RouteRecordRaw[] = []
  for (const route of routes) {
    const rawPath = String(route.path ?? '')
    const joined = rawPath.startsWith('/') ? rawPath : `${prefix}/${rawPath}`
    out.push({ ...route, path: joined.replace(/\/{2,}/g, '/') })
    if (route.children?.length) out.push(...flattenRoutes(route.children, joined))
  }
  return out
}

/**
 * 解析助手回复中的导航指令。
 * @param content - 助手回复 markdown 原文
 * @returns { cleaned, directives } cleaned 为剥离指令后的展示文本
 */
export function extractNavigateDirectives(
  content: string,
): { cleaned: string; directives: AgentNavigateDirective[] } {
  if (!content || !content.includes('PRISM_NAVIGATE')) {
    return { cleaned: content ?? '', directives: [] }
  }
  const directives: AgentNavigateDirective[] = []
  let changed = false
  const cleaned = transformOutsideCodeFences(content, (segment) => (
    segment.replace(NAVIGATE_DIRECTIVE_RE, (_all, raw: string) => {
      changed = true
      const json = String(raw ?? '').trim()
      if (!json || json.length > MAX_DIRECTIVE_JSON_LENGTH) return ''
      try {
        const parsed = JSON.parse(json) as Record<string, unknown>
        if (parsed?.action !== 'navigate') return ''
        const route = typeof parsed.route === 'string' ? parsed.route : ''
        if (!route.startsWith('/') || route.startsWith('//')) return ''
        directives.push({
          action: 'navigate',
          route,
          label: typeof parsed.label === 'string' ? parsed.label.slice(0, 40) : undefined,
          hint: typeof parsed.hint === 'string' ? parsed.hint.slice(0, 120) : undefined,
        })
      } catch {
        // 非法 JSON 直接丢弃,不阻断正文渲染
      }
      return ''
    })
  ))
  if (!changed) return { cleaned: content, directives: [] }
  return {
    cleaned: normalizeAgentText(cleaned),
    directives,
  }
}

/**
 * 当模型遗漏协议注释但已在导航语境中给出真实路由时，生成受鉴权按钮。
 * 只识别代码围栏外、命中静态路由表的路径；未知地址和代码样例不会变成入口。
 */
export function inferKnownRouteDirectives(
  content: string,
  router: Pick<Router, 'options'>,
): AgentNavigateDirective[] {
  if (!content || !NAVIGATION_CUE_RE.test(content)) return []
  const routeTable = buildRouteTable(router as Router)
  const directives: AgentNavigateDirective[] = []
  const seen = new Set<string>()
  transformOutsideCodeFences(content, (segment) => {
    for (const line of segment.split('\n')) {
      if (!NAVIGATION_CUE_RE.test(line)) continue
      for (const match of line.matchAll(KNOWN_ROUTE_RE)) {
        const route = match[0]
        const pathname = route.split(/[?#]/, 1)[0]
        const label = routeTable.get(pathname)
        if (!label || seen.has(route)) continue
        seen.add(route)
        directives.push({ action: 'navigate', route, label })
      }
    }
    return segment
  })
  return directives
}

/** 优先解析显式协议，协议缺失时才从已知路由导航语境中安全兜底。 */
export function extractAgentNavigations(
  content: string,
  router: Pick<Router, 'options'>,
): { cleaned: string; directives: AgentNavigateDirective[] } {
  const parsed = extractNavigateDirectives(content)
  if (parsed.directives.length) return parsed
  return {
    cleaned: parsed.cleaned,
    directives: inferKnownRouteDirectives(parsed.cleaned, router),
  }
}

/** 判断导航指令是否为单一路由(可自动跳转)。 */
export function isAutoNavigateDirective(directives: AgentNavigateDirective[]): boolean {
  return directives.length === 1 && directives[0].route !== ''
}

/** 判断路由守卫是否会放行目标路由(未命中守卫规则视为放行)。 */
export function isRouteAllowed(resolved: {
  meta?: RouteMeta
}, guardSource: {
  token: string
  isAdmin: () => boolean
  isSuperAdmin: () => boolean
  hasRole: (role: string) => boolean
  hasPermission: (code: string) => boolean
}): boolean {
  const meta = resolved.meta ?? {}
  if (meta.public) return true
  if (!guardSource.token) return false
  if (meta.superAdmin && !guardSource.isSuperAdmin()) return false
  const isAdmin = guardSource.isAdmin()
  if (meta.role && !isAdmin && !guardSource.hasRole(meta.role)) return false
  if (meta.roles?.length && !isAdmin && !meta.roles.some((role) => guardSource.hasRole(role))) return false
  if (
    meta.permissions?.length
    && !isAdmin
    && !meta.permissions.some((code) => guardSource.hasPermission(code))
  ) return false
  return true
}

/**
 * 供菜单、搜索与 Agent 导航共用的站内路由可见性判定。
 * 未注册路由、外部路径或当前用户守卫不能放行时一律不展示入口。
 */
export function isNavigationPathAllowed(
  router: Pick<Router, 'resolve'>,
  path: string,
  guardSource: Parameters<typeof isRouteAllowed>[1],
): boolean {
  if (!path.startsWith('/') || path.startsWith('//')) return false
  try {
    const pathname = path.split(/[?#]/, 1)[0]
    const resolved = router.resolve({ path: pathname })
    if (!resolved.matched.length) return false
    if (resolved.matched.some((record) => String(record.path ?? '').includes(':pathMatch'))) return false
    return isRouteAllowed(resolved, guardSource)
  } catch {
    return false
  }
}

/**
 * 渲染小菱正文中的 Markdown,并移除当前账号不能打开的站内链接。
 * 外部链接保持原有展示规则,由 MarkdownIt/DOMPurify 负责安全处理。
 */
export function renderAuthorizedAgentMarkdown(
  content: string,
  router: Pick<Router, 'resolve'>,
  guardSource: Parameters<typeof isRouteAllowed>[1],
): string {
  return renderMarkdown(content, {
    linkAllowed: (href) => (
      !href.startsWith('/')
      || href.startsWith('//')
      || isNavigationPathAllowed(router, href, guardSource)
    ),
  })
}

/** 从应用路由表构建可引导的站内路由集合(含嵌套 children)。 */
export function collectKnownRoutes(router: Router): Set<string> {
  const routes = router.options?.routes ?? []
  return new Set(flattenRoutes(routes, '').map((route) => String(route.path ?? '')))
}

/**
 * 建立站内链接白名单,以及路由 → 中文标题映射(用于把裸链接渲染成引导卡片)。
 */
export function buildRouteTable(router: Router): Map<string, string> {
  const table = new Map<string, string>()
  for (const route of flattenRoutes(router.options?.routes ?? [], '')) {
    const path = String(route.path ?? '')
    if (!path || path.includes(':') || path.includes('*')) continue
    table.set(path, String(route.meta?.title ?? ''))
  }
  return table
}
