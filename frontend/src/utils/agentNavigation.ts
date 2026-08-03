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

const NAVIGATE_DIRECTIVE_RE = /<!--\s*PRISM_NAVIGATE\s*([\s\S]*?)\s*-->/g
const MAX_DIRECTIVE_JSON_LENGTH = 2048

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
  if (meta.role && meta.role !== 'admin' && !isAdmin) return false
  if (meta.role === 'admin' && !isAdmin) return false
  if (meta.roles?.length && !isAdmin && !meta.roles.some((role) => guardSource.hasRole(role))) return false
  if (
    meta.permissions?.length
    && !isAdmin
    && !meta.permissions.some((code) => guardSource.hasPermission(code))
  ) return false
  return true
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
