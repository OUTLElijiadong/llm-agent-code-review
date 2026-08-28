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
const MAX_LOCAL_NAVIGATION_TEXT_LENGTH = 180
const KNOWN_ROUTE_RE = /\/[a-z0-9][a-z0-9/_-]*(?:\?[a-z0-9%&=_.~-]+)?(?:#[a-z0-9%_.~-]+)?/gi
const NAVIGATION_CUE_RE = /路由|页面|导航|前往|进入|打开|带(?:我|你)去/
const LOCAL_NAVIGATION_PREFIX_RE = /^(?:小菱)?(?:请帮我|请|帮我|麻烦|可以)?(?:只(?:执行|做)?页面导航|仅(?:执行|做)?页面导航|页面导航)?(?:打开|前往|进入|跳转到|导航到|带我去|把我带到|去)(?:一下)?$/

/** 常用简称只作为已有静态路由标题的别名,不新增可访问路径。 */
const LOCAL_NAVIGATION_ALIASES: Record<string, string[]> = {
  '/admin/overview': ['总览', '管理总览'],
  '/admin/approvals': ['审批'],
  '/admin/observability': ['监控', '告警'],
  '/admin/users': ['用户'],
  '/admin/audit': ['审计', '系统审计'],
  '/admin/jobs': ['调度'],
  '/admin/rollback': ['回滚'],
  '/admin/ai-logs': ['调用日志'],
  '/admin/mcp-workers': ['节点'],
  '/projects': ['项目'],
  '/code': ['代码'],
  '/reviews': ['审查'],
  '/issues': ['问题'],
  '/reports': ['报告'],
  '/security': ['安全'],
  '/sandboxes': ['沙箱'],
  '/agents': ['Agent'],
  '/knowledge': ['知识'],
  '/profile': ['个人中心'],
}

export interface LocalNavigationResult {
  kind: 'navigate' | 'forbidden'
  directive?: AgentNavigateDirective
}

function compactNavigationText(value: string): string {
  return value
    .replace(/[\s\u200b\uFEFF]/g, '')
    .replace(/[，,。.!！?？:：;；、"“”‘’`~～（）()【】[\]<>]/g, '')
}

function isSafeLocalNavigationTail(value: string): boolean {
  const tail = value
    .replace(/^(?:页面|页|界面|菜单)/, '')
    .replace(/^(?:即可|就行|就好|吧)+/, '')
  // “不要查询/修改/执行……”是对本地导航的明确边界说明,不代表要执行这些动作。
  if (/^(?:不要|无需|不需要|不用)/.test(tail)) return true
  return tail === '' || /^(?:一下)*(?:完成后(?:回复|告诉我|返回)?(?:已打开|打开成功|完成)?)?$/.test(tail)
}

function localNavigationTargets(router: Pick<Router, 'options'>): Array<{ route: string; label: string; matchLabel: string }> {
  const table = buildRouteTable(router as Router)
  const targets: Array<{ route: string; label: string; matchLabel: string }> = []
  for (const [route, label] of table.entries()) {
    if (!label) continue
    targets.push({ route, label, matchLabel: label })
    for (const alias of LOCAL_NAVIGATION_ALIASES[route] ?? []) {
      targets.push({ route, label, matchLabel: alias })
    }
  }
  return targets.sort((left, right) => right.matchLabel.length - left.matchLabel.length)
}

/**
 * 识别“只打开一个页面”的确定性请求。
 * 这类请求不需要调用模型,直接复用当前路由守卫并交给虚拟鼠标执行,
 * 避免一次纯导航产生多轮付费 Responses 调用。
 */
export function resolveLocalNavigationRequest(
  content: string,
  router: Pick<Router, 'options' | 'resolve'>,
  guardSource: Parameters<typeof isRouteAllowed>[1],
): LocalNavigationResult | null {
  if (!content || content.length > MAX_LOCAL_NAVIGATION_TEXT_LENGTH) return null
  const compact = compactNavigationText(content)
  if (!compact) return null

  const seenRoutes = new Set<string>()
  for (const target of localNavigationTargets(router)) {
    if (seenRoutes.has(target.route)) continue
    const matchLabel = compactNavigationText(target.matchLabel)
    const start = compact.indexOf(matchLabel)
    if (start < 0) continue
    const prefix = compact.slice(0, start)
    const tail = compact.slice(start + matchLabel.length)
    if (!LOCAL_NAVIGATION_PREFIX_RE.test(prefix) || !isSafeLocalNavigationTail(tail)) continue
    seenRoutes.add(target.route)
    if (!isNavigationPathAllowed(router, target.route, guardSource)) return { kind: 'forbidden' }
    return {
      kind: 'navigate',
      directive: { action: 'navigate', route: target.route, label: target.label },
    }
  }
  return null
}

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
