import { expect, test, type Page } from '@playwright/test'

const COMPANY_ROOM = {
  id: 9, enterprise_id: 10, kind: 'company', group_id: null,
  participant_low_id: null, participant_high_id: null, create_time: null,
}
const GROUP_ROOM = {
  id: 20, enterprise_id: 10, kind: 'group', group_id: 22,
  participant_low_id: null, participant_high_id: null, create_time: null,
}

type EnterpriseFixture = {
  companyRole: 'owner' | 'admin' | 'member'
  groupRevoked: boolean
}

async function installEnterpriseApi(page: Page, fixture: EnterpriseFixture): Promise<void> {
  await page.addInitScript(() => localStorage.setItem('review_token', 'enterprise-focus-sync-fixture'))
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    const method = request.method()
    if (!pathname.startsWith('/api/')) return route.continue()
    let data: unknown = []

    if (pathname === '/api/auth/me') {
      data = { id: 1, username: '浏览器验收成员', nickname: '成员', role: 'user', status: 1 }
    } else if (pathname === '/api/rbac/users/1/roles') {
      data = [{ code: 'user' }]
    } else if (pathname === '/api/rbac/users/1/permissions' || pathname === '/api/rbac/users/1/menus') {
      data = []
    } else if (pathname === '/api/rbac/users/1/data-scope') {
      data = null
    } else if (pathname === '/api/me/profile') {
      data = { user_id: 1, hobbies: '', goals: '', tech_stack: '', focus_areas: [],
        preferred_language: 'zh-CN', experience_level: 'beginner', auto_learn: false,
        derived_summary: '', derived_stats: {} }
    } else if (pathname === '/api/enterprises/mine') {
      data = [{ id: 10, name: '浏览器验收企业', status: 'active', role: fixture.companyRole,
        default_group_id: null, subscription_status: 'active' }]
    } else if (pathname === '/api/enterprises/10/groups') {
      data = fixture.groupRevoked ? [] : [{ id: 22, enterprise_id: 10, name: '研发组',
        status: 'active', my_role: 'leader' }]
    } else if (pathname === '/api/enterprises/10/directory') {
      data = [{ user_id: 1, display_name: '成员' }, { user_id: 2, display_name: '同事' }]
    } else if (pathname === '/api/enterprises/10/conversations' && method === 'GET') {
      data = { items: fixture.groupRevoked ? [COMPANY_ROOM] : [COMPANY_ROOM, GROUP_ROOM], assistant_available: false }
    } else if (pathname === '/api/enterprises/10/conversations' && method === 'POST') {
      const body = request.postDataJSON() as { kind?: string; group_id?: number }
      data = body.kind === 'group' ? GROUP_ROOM : COMPANY_ROOM
    } else if (pathname === '/api/enterprises/10/conversations/20/messages') {
      data = { items: fixture.groupRevoked ? [] : [{ id: 1, conversation_id: 20, sender_id: 2, sender_type: 'human',
        client_message_id: 'fixture-message', content: '只属于研发组的内容', create_time: null }], assistant_available: false }
    } else if (pathname === '/api/enterprises/10/conversations/20/assistant-runs') {
      data = { items: [], assistant_mode: 'enterprise_agent_runtime' }
    }

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ code: 0, message: 'ok', data }),
    })
  })
}

test('企业管理员升降权后焦点同步更新管理入口', async ({ page }) => {
  const fixture: EnterpriseFixture = { companyRole: 'member', groupRevoked: true }
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await installEnterpriseApi(page, fixture)
  await page.goto('/enterprise')
  await expect.poll(() => page.url()).toContain('/enterprise')
  await expect(page.locator('#app')).not.toBeEmpty({ timeout: 15_000 })
  await expect(page.getByRole('heading', { name: '企业空间' })).toBeVisible()
  await expect(page.getByRole('button', { name: '组织管理' })).toHaveCount(0)

  fixture.companyRole = 'admin'
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await expect(page.getByRole('button', { name: '组织管理' })).toBeVisible()
  await expect(page.getByRole('button', { name: '企业用量' })).toBeVisible()
  await expect(page.getByRole('button', { name: '小菱审批' })).toBeVisible()

  await page.getByRole('button', { name: '组织管理' }).click()
  await expect(page.getByRole('button', { name: '组织管理' })).toHaveAttribute('aria-pressed', 'true')
  fixture.companyRole = 'member'
  await page.waitForTimeout(1_050)
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await expect(page.getByRole('button', { name: '组织管理' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '企业用量' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '小菱审批' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '聊天' })).toHaveAttribute('aria-pressed', 'true')
  expect(errors).toEqual([])
})

test('小组长失去小组成员资格后真实浏览器清空群聊和旧内容', async ({ page }) => {
  const fixture: EnterpriseFixture = { companyRole: 'member', groupRevoked: false }
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await installEnterpriseApi(page, fixture)
  await page.goto('/enterprise')
  await expect(page.getByRole('button', { name: '研发组', exact: true })).toBeVisible()
  await page.locator('.conversation-quick-actions button').filter({ hasText: '研发组' }).click()
  await expect(page.getByText('只属于研发组的内容')).toBeVisible()

  fixture.groupRevoked = true
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
  await expect(page.getByText('小组会话权限已变化')).toBeVisible()
  await expect(page.getByText('只属于研发组的内容')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '研发组', exact: true })).toHaveCount(0)
  expect(errors).toEqual([])
})
