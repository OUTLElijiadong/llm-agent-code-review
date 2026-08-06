import { expect, it, vi } from 'vitest'

import {
  canAdminOpenPath,
  canRoleOpenPath,
  canRoleSeeNavigationItem,
  getRoleHomePath,
  normalizeRole,
  resolvePostLoginPath,
} from '@/utils/roleHome'
import {
  formatDate,
  formatDateTime,
  formatDuration,
  formatFileSize,
  formatNumber,
  formatPercent,
  formatRelativeTime,
} from '@/utils/format'
import { detectLanguage, SUPPORTED_LANGUAGES } from '@/utils/language'
import { goBack } from '@/utils/navigation'
import { renderMarkdown, stripMarkdown } from '@/utils/markdown'
import { clearToken, getToken, setToken } from '@/utils/token'

/** 验证角色归一化与默认首页映射。 */
it('normalizes known roles and falls back to the user home', function testRoleNormalization(): void {
  expect(normalizeRole('admin')).toBe('admin')
  expect(normalizeRole('reviewer')).toBe('reviewer')
  expect(normalizeRole('user')).toBe('user')
  expect(normalizeRole('ADMIN')).toBe('user')
  expect(normalizeRole(null)).toBe('user')
  expect(normalizeRole(undefined)).toBe('user')
  expect(getRoleHomePath('admin')).toBe('/admin/overview')
  expect(getRoleHomePath('reviewer')).toBe('/dashboard')
  expect(getRoleHomePath('unknown')).toBe('/dashboard')
})

/** 验证管理员可进入全部已认证主站页面且仍拒绝非站内路径。 */
it('enforces the administrator route allowlist', function testAdminRouteAllowlist(): void {
  expect(canAdminOpenPath('/dashboard')).toBe(true)
  expect(canAdminOpenPath('/projects')).toBe(true)
  expect(canAdminOpenPath('/code')).toBe(true)
  expect(canAdminOpenPath('/rules')).toBe(true)
  expect(canAdminOpenPath('/knowledge')).toBe(true)
  expect(canAdminOpenPath('/profile/personalization')).toBe(true)
  expect(canAdminOpenPath('/agents/logs')).toBe(true)
  expect(canAdminOpenPath('/security')).toBe(true)
  expect(canAdminOpenPath('/forum/post/1')).toBe(true)
  expect(canAdminOpenPath('/support/tickets')).toBe(true)
  expect(canAdminOpenPath('/admin/users')).toBe(true)
  expect(canAdminOpenPath('/profile')).toBe(true)
  expect(canAdminOpenPath('/profile/password')).toBe(true)
  expect(canAdminOpenPath('/administrator')).toBe(false)
})

/** 验证主菜单与全局搜索共享统一的角色可见性语义。 */
it('lets administrators see all authenticated navigation items', function testNavigationVisibility(): void {
  expect(canRoleSeeNavigationItem('admin', ['user'])).toBe(true)
  expect(canRoleSeeNavigationItem('admin', ['reviewer'])).toBe(true)
  expect(canRoleSeeNavigationItem('user', ['user'])).toBe(true)
  expect(canRoleSeeNavigationItem('user', ['reviewer'])).toBe(false)
  expect(canRoleSeeNavigationItem('reviewer')).toBe(true)
})

/** 验证角色路由守卫拒绝外部、认证页与后台越权路径。 */
it('rejects unsafe or role-incompatible direct routes', function testRoleRouteGuard(): void {
  expect(canRoleOpenPath('user', 'dashboard')).toBe(false)
  expect(canRoleOpenPath('user', '//evil.example/path')).toBe(false)
  expect(canRoleOpenPath('user', '/admin')).toBe(false)
  expect(canRoleOpenPath('reviewer', '/admin/users')).toBe(false)
  expect(canRoleOpenPath('admin', '/admin/users')).toBe(true)
  expect(canRoleOpenPath('user', '/login')).toBe(false)
  expect(canRoleOpenPath('user', '/register')).toBe(false)
  expect(canRoleOpenPath('admin', '/projects')).toBe(true)
  expect(canRoleOpenPath('admin', '/profile/personalization')).toBe(true)
  expect(canRoleOpenPath('user', '/projects/1')).toBe(true)
})

/** 验证登录后重定向只保留当前角色可访问的内部地址。 */
it('resolves post-login redirects to a safe role home', function testPostLoginRedirect(): void {
  expect(resolvePostLoginPath('admin')).toBe('/admin/overview')
  expect(resolvePostLoginPath('admin', '/')).toBe('/admin/overview')
  expect(resolvePostLoginPath('admin', '/dashboard')).toBe('/admin/overview')
  expect(resolvePostLoginPath('user', '/login?redirect=/projects')).toBe('/dashboard')
  expect(resolvePostLoginPath('user', '/register')).toBe('/dashboard')
  expect(resolvePostLoginPath('user', '/admin/users')).toBe('/dashboard')
  // 服务器版:管理员登录后非 /admin 重定向一律落总览大屏
  expect(resolvePostLoginPath('admin', '/projects')).toBe('/admin/overview')
  expect(resolvePostLoginPath('reviewer', '/projects/7')).toBe('/projects/7')
})

/** 验证日期、相对时间与空值格式化。 */
it('formats dates and relative times deterministically', function testDateFormatting(): void {
  vi.useFakeTimers()
  const now = new Date(2026, 0, 2, 4, 4, 5)
  const oneHourAgo = new Date(2026, 0, 2, 3, 4, 5)
  vi.setSystemTime(now)

  expect(formatDateTime(null)).toBe('-')
  expect(formatDateTime(new Date(2026, 0, 2, 3, 4, 5))).toBe('2026-01-02 03:04:05')
  expect(formatDateTime(new Date(2026, 0, 2, 3, 4, 5), 'MM/DD HH:mm')).toBe('01/02 03:04')
  expect(formatDate(undefined)).toBe('-')
  expect(formatDate(new Date(2026, 0, 2))).toBe('2026-01-02')
  expect(formatRelativeTime(null)).toBe('-')
  expect(formatRelativeTime(oneHourAgo)).toContain('小时前')
})

/** 验证文件大小与时长的正常、边界和空值分支。 */
it('formats file sizes and durations across unit boundaries', function testSizeAndDurationFormatting(): void {
  expect(formatFileSize(undefined)).toBe('0 B')
  expect(formatFileSize(-1)).toBe('0 B')
  expect(formatFileSize(0)).toBe('0 B')
  expect(formatFileSize(1023)).toBe('1023 B')
  expect(formatFileSize(1536, 1)).toBe('1.5 KB')
  expect(formatFileSize(1024 ** 2)).toBe('1 MB')

  expect(formatDuration(undefined)).toBe('-')
  expect(formatDuration(-1)).toBe('-')
  expect(formatDuration(0)).toBe('-')
  expect(formatDuration(999)).toBe('0秒')
  expect(formatDuration(59_000)).toBe('59秒')
  expect(formatDuration(60_000)).toBe('1分钟')
  expect(formatDuration(61_000)).toBe('1分1秒')
  expect(formatDuration(3_600_000)).toBe('1小时')
  expect(formatDuration(3_660_000)).toBe('1小时1分')
})

/** 验证数字和百分比格式化保留零值并处理空值。 */
it('formats numeric values without dropping zero', function testNumericFormatting(): void {
  expect(formatNumber(null)).toBe('-')
  expect(formatNumber(0)).toBe('0')
  expect(formatNumber(1_234_567)).toBe('1,234,567')
  expect(formatPercent(undefined)).toBe('-')
  expect(formatPercent(0)).toBe('0.0%')
  expect(formatPercent(0.855)).toBe('85.5%')
  expect(formatPercent(0.855, 0)).toBe('86%')
})

/** 验证扩展名映射、大小写处理、未知文件和 Dockerfile 特例。 */
it('detects Monaco language identifiers from common file names', function testLanguageDetection(): void {
  expect(detectLanguage('src/main.PY')).toBe('python')
  expect(detectLanguage('component.tsx')).toBe('typescript')
  expect(detectLanguage('styles.SASS')).toBe('scss')
  expect(detectLanguage('config.yml')).toBe('yaml')
  expect(detectLanguage('Dockerfile')).toBe('dockerfile')
  expect(detectLanguage('/workspace/Dockerfile')).toBe('dockerfile')
  expect(detectLanguage('C:\\repo\\Dockerfile')).toBe('dockerfile')
  expect(detectLanguage('')).toBe('plaintext')
  expect(detectLanguage('README')).toBe('plaintext')
  expect(detectLanguage('.gitignore')).toBe('plaintext')
  expect(detectLanguage('config.')).toBe('plaintext')
  expect(detectLanguage('archive.unknown')).toBe('plaintext')
  expect(SUPPORTED_LANGUAGES).toContain('dockerfile')
  expect(SUPPORTED_LANGUAGES).toContain('python')
  expect(SUPPORTED_LANGUAGES).toEqual([...SUPPORTED_LANGUAGES].sort())
  expect(new Set(SUPPORTED_LANGUAGES).size).toBe(SUPPORTED_LANGUAGES.length)
})

/** 验证 token 本地存储的读取、覆盖和清理。 */
it('stores and clears the authentication token in localStorage', function testTokenStorage(): void {
  expect(getToken()).toBeNull()
  setToken('token-one')
  expect(getToken()).toBe('token-one')
  setToken('token-two')
  expect(getToken()).toBe('token-two')
  clearToken()
  expect(getToken()).toBeNull()
})

/** 验证直接访问页面时的站内返回与外部历史防护。 */
it('uses an in-app history entry or a safe fallback when going back', function testGoBack(): void {
  const router = { back: vi.fn(), replace: vi.fn() }
  window.history.replaceState({ back: '/previous' }, '', '/app/current')
  goBack(router as never, '/dashboard')
  expect(router.back).toHaveBeenCalledOnce()
  expect(router.replace).not.toHaveBeenCalled()

  window.history.replaceState(null, '', '/app/current')
  goBack(router as never, '/dashboard')
  expect(router.replace).toHaveBeenCalledWith('/dashboard')
})

/** 验证 AI Markdown 中的表格能渲染且原生 HTML 会被安全过滤。 */
it('renders safe Markdown and strips formatting for plain-text summaries', function testMarkdown(): void {
  const html = renderMarkdown('| ID | 用户名 |\n| --- | --- |\n| 5 | lijiadong |\n<script>alert(1)</script>')
  expect(html).toContain('<table>')
  expect(html).toContain('lijiadong')
  expect(html).not.toContain('<script>')
  expect(stripMarkdown('**结论**\n\n- 通过')).toContain('结论')
  expect(renderMarkdown(undefined as unknown as string)).toBe('')
  expect(stripMarkdown(undefined as unknown as string)).toBe('')
})
