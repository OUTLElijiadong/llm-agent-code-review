import { describe, expect, it } from 'vitest'

import {
  extractNavigateDirectives,
  isAutoNavigateDirective,
  isRouteAllowed,
} from '@/utils/agentNavigation'

function makeGuardSource(overrides: Partial<Parameters<typeof isRouteAllowed>[1]> = {}) {
  return {
    token: 'token',
    isAdmin: () => false,
    isSuperAdmin: () => false,
    hasRole: (role: string) => role === 'reviewer',
    hasPermission: (code: string) => code === 'project:view',
    ...overrides,
  }
}

describe('extractNavigateDirectives', () => {
  it('剥离单一导航指令并解析路由', () => {
    const raw = '好的，项目已创建。\n<!--PRISM_NAVIGATE {"action":"navigate","route":"/projects","label":"项目管理"}-->'
    const { cleaned, directives } = extractNavigateDirectives(raw)
    expect(cleaned).toBe('好的，项目已创建。')
    expect(directives).toEqual([
      { action: 'navigate', route: '/projects', label: '项目管理', hint: undefined },
    ])
    expect(isAutoNavigateDirective(directives)).toBe(true)
  })

  it('忽略 action 不是 navigate 的注释', () => {
    const raw = '正文 <!--PRISM_NAVIGATE {"action":"delete","route":"/projects"}-->'
    const { directives } = extractNavigateDirectives(raw)
    expect(directives).toEqual([])
  })

  it('拒绝非站内路由(协议相对/绝对 URL)', () => {
    const raw = '<!--PRISM_NAVIGATE {"action":"navigate","route":"//evil.com/x"}-->'
    const { directives } = extractNavigateDirectives(raw)
    expect(directives).toEqual([])
  })

  it('拒绝非 JSON 内容', () => {
    const raw = '<!--PRISM_NAVIGATE not-json-->'
    const { directives } = extractNavigateDirectives(raw)
    expect(directives).toEqual([])
  })

  it('多指令不算自动跳转', () => {
    const raw = [
      '<!--PRISM_NAVIGATE {"action":"navigate","route":"/projects"}-->',
      '<!--PRISM_NAVIGATE {"action":"navigate","route":"/reviews"}-->',
    ].join('\n')
    const { directives } = extractNavigateDirectives(raw)
    expect(directives).toHaveLength(2)
    expect(isAutoNavigateDirective(directives)).toBe(false)
  })

  it('无指令时原样返回', () => {
    const raw = '普通回复,不含任何指令'
    const { cleaned, directives } = extractNavigateDirectives(raw)
    expect(cleaned).toBe(raw)
    expect(directives).toEqual([])
  })

  it('does not parse or rewrite navigation examples inside code fences', () => {
    const raw = [
      '````md',
      '<!--PRISM_NAVIGATE {"action":"navigate","route":"/projects"}-->',
      '```',
      '',
      '代码仍在四反引号围栏内',
      '````',
    ].join('\n')

    expect(extractNavigateDirectives(raw)).toEqual({ cleaned: raw, directives: [] })
  })

  it('removes an outside directive without changing blank lines inside code fences', () => {
    const code = '````md\n代码前\n```\n\n代码后\n````'
    const raw = `${code}\n\n<!--PRISM_NAVIGATE {"action":"navigate","route":"/projects"}-->`
    const { cleaned, directives } = extractNavigateDirectives(raw)

    expect(cleaned).toBe(code)
    expect(directives).toHaveLength(1)
  })
})

describe('isRouteAllowed', () => {
  const resolved = (meta: Record<string, unknown>) =>
    ({ meta, matched: [{}], fullPath: '/x', path: '/x' }) as never

  it('公开页面放行', () => {
    expect(isRouteAllowed(resolved({ public: true }), makeGuardSource({ token: '' }))).toBe(true)
  })

  it('未登录拒绝受保护页面', () => {
    expect(isRouteAllowed(resolved({}), makeGuardSource({ token: '' }))).toBe(false)
  })

  it('admin 专属页面对普通用户拒绝', () => {
    expect(isRouteAllowed(resolved({ role: 'admin' }), makeGuardSource())).toBe(false)
  })

  it('admin 角色放行 admin 页面', () => {
    expect(
      isRouteAllowed(resolved({ role: 'admin' }), makeGuardSource({ isAdmin: () => true })),
    ).toBe(true)
  })

  it('roles 命中任一角色即放行', () => {
    expect(isRouteAllowed(resolved({ roles: ['reviewer', 'admin'] }), makeGuardSource())).toBe(true)
  })

  it('roles 未命中拒绝', () => {
    expect(isRouteAllowed(resolved({ roles: ['admin'] }), makeGuardSource())).toBe(false)
  })

  it('permissions 命中权限点放行', () => {
    expect(isRouteAllowed(resolved({ permissions: ['project:view'] }), makeGuardSource())).toBe(true)
  })

  it('permissions 未命中拒绝', () => {
    expect(isRouteAllowed(resolved({ permissions: ['user:delete'] }), makeGuardSource())).toBe(false)
  })

  it('superAdmin 页面仅超管放行', () => {
    expect(isRouteAllowed(resolved({ superAdmin: true }), makeGuardSource())).toBe(false)
    expect(
      isRouteAllowed(
        resolved({ superAdmin: true }),
        makeGuardSource({ isSuperAdmin: () => true, isAdmin: () => true }),
      ),
    ).toBe(true)
  })
})
