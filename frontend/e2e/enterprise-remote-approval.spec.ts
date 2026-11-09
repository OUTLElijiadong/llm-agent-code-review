import { expect, test, type Browser, type BrowserContext, type Page } from '@playwright/test'

type RemoteRequest = {
  id: number
  enterprise_id: number
  group_id: number
  project_id: number
  owner_id: number
  target_url: string
  target_host: string
  authorization_basis: string
  scope_notes: string
  requested_window_hours: number
  status: 'pending' | 'approved' | 'rejected' | 'cancelled'
  decided_by: number | null
  decision_reason: string | null
  decided_at: string | null
  create_time: string
  execution_task_public_id: string | null
  execution_available: boolean
}

type Account = { id: number; name: string; enterpriseRole: 'owner' | 'admin' | 'member' }

const shared = {
  requests: [] as RemoteRequest[],
  nextId: 1,
  startCalls: 0,
  targetRequests: 0,
}

async function installAccountApi(context: BrowserContext, page: Page, account: Account): Promise<void> {
  await context.addInitScript((token) => localStorage.setItem('review_token', token), `remote-approval-${account.id}`)
  page.on('request', (request) => {
    if (new URL(request.url()).hostname === 'authorized.example.com') shared.targetRequests += 1
  })
  page.on('dialog', async (dialog) => {
    await dialog.accept(dialog.type() === 'prompt' ? '已复核企业授权、项目和固定范围' : undefined)
  })
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    const method = request.method()
    if (!pathname.startsWith('/api/')) return route.continue()

    let data: unknown = []
    if (pathname === '/api/auth/me') {
      data = { id: account.id, username: `remote-user-${account.id}`, nickname: account.name, role: 'user', status: 1 }
    } else if (pathname === `/api/rbac/users/${account.id}/roles`) {
      data = [{ code: 'user' }]
    } else if (pathname === `/api/rbac/users/${account.id}/permissions`) {
      data = ['pentest:view', 'pentest:start']
    } else if (pathname === `/api/rbac/users/${account.id}/menus`) {
      data = []
    } else if (pathname === `/api/rbac/users/${account.id}/data-scope`) {
      data = null
    } else if (pathname === '/api/me/profile') {
      data = { user_id: account.id, hobbies: '', goals: '', tech_stack: '', focus_areas: [],
        preferred_language: 'zh-CN', experience_level: 'beginner', auto_learn: false,
        derived_summary: '', derived_stats: {} }
    } else if (pathname === '/api/enterprises/mine') {
      data = [{ id: 10, name: '浏览器合成验收企业', status: 'active', role: account.enterpriseRole,
        default_group_id: null, subscription_status: 'active' }]
    } else if (pathname === '/api/enterprises/10/groups') {
      data = [{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'member' }]
    } else if (pathname === '/api/enterprises/10/directory') {
      data = [
        { user_id: 1, display_name: '企业所有者' },
        { user_id: 2, display_name: '申请成员' },
      ]
    } else if (pathname === '/api/enterprises/10/conversations' && method === 'GET') {
      data = { items: [], assistant_available: false }
    } else if (pathname === '/api/enterprises/10/projects') {
      data = [{ project_id: 500, project_name: '研发组代码审查', enterprise_id: 10,
        group_id: 22, group_name: '研发组', role: account.enterpriseRole === 'member' ? 'reviewer' : 'admin' }]
    } else if (pathname === '/api/enterprises/10/remote-test-requests' && method === 'GET') {
      const visible = account.enterpriseRole === 'owner' || account.enterpriseRole === 'admin'
        ? shared.requests
        : shared.requests.filter((item) => item.owner_id === account.id)
      const query = new URL(request.url()).searchParams
      const offset = Number(query.get('offset') || 0)
      const limit = Number(query.get('limit') || 20)
      data = { items: visible.slice(offset, offset + limit), total: visible.length }
    } else if (pathname === '/api/enterprises/10/remote-test-requests' && method === 'POST') {
      const body = request.postDataJSON() as {
        group_id: number; project_id: number; target_url: string; authorization_basis: string;
        scope_notes: string; requested_window_hours: number
      }
      const url = new URL(body.target_url)
      const row: RemoteRequest = {
        id: shared.nextId++, enterprise_id: 10, group_id: body.group_id, project_id: body.project_id,
        owner_id: account.id, target_url: body.target_url, target_host: url.hostname,
        authorization_basis: body.authorization_basis, scope_notes: body.scope_notes,
        requested_window_hours: body.requested_window_hours, status: 'pending', decided_by: null,
        decision_reason: null, decided_at: null, create_time: '2026-11-07T08:00:00Z',
        execution_task_public_id: null, execution_available: false,
      }
      shared.requests.unshift(row)
      data = row
    } else if (/^\/api\/enterprises\/10\/remote-test-requests\/\d+\/decision$/.test(pathname) && method === 'POST') {
      const id = Number(pathname.split('/').at(-2))
      const body = request.postDataJSON() as { decision: 'approved' | 'rejected'; reason: string }
      const row = shared.requests.find((item) => item.id === id)
      if (row) {
        row.status = body.decision
        row.decided_by = account.id
        row.decision_reason = body.reason
        row.decided_at = '2026-11-07T08:05:00Z'
        row.execution_available = body.decision === 'approved'
      }
      data = row ?? null
    } else if (/^\/api\/enterprises\/10\/remote-test-requests\/\d+\/start$/.test(pathname) && method === 'POST') {
      shared.startCalls += 1
      const id = Number(pathname.split('/').at(-2))
      const row = shared.requests.find((item) => item.id === id)
      if (row) row.execution_task_public_id = 'synthetic-remote-task'
      data = { request: row, execution: { public_id: 'synthetic-remote-task', status: 'queued' } }
    } else if (pathname === '/api/remote-pentest/tasks/synthetic-remote-task') {
      data = {
        id: 900, public_id: 'synthetic-remote-task', owner_id: 2, task_name: '合成远测浏览器验收',
        target_url: 'https://authorized.example.com/', target_host: 'authorized.example.com',
        status: 'authorized', rules_version: 'remote-pentest-safe-v2', rules_ack: 1,
        requested_window_hours: 4, summary: {}, phases: [], findings: [],
      }
    }

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ code: 0, message: 'ok', data }),
    })
  })
}

async function createAccountPage(browser: Browser, account: Account): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  const page = await context.newPage()
  await installAccountApi(context, page, account)
  await page.goto('/enterprise')
  await expect(page.getByRole('heading', { name: '企业空间' })).toBeVisible()
  await page.getByRole('button', { name: '远测申请与审批' }).click()
  await expect(page.getByRole('heading', { name: '企业远测申请与审批' })).toBeVisible()
  return { context, page }
}

test.beforeEach(() => {
  shared.requests = []
  shared.nextId = 1
  shared.startCalls = 0
  shared.targetRequests = 0
})

test('成员申请、owner 审批、申请人主动启动形成跨账号浏览器闭环', async ({ browser }) => {
  test.setTimeout(30_000)
  const member = await createAccountPage(browser, { id: 2, name: '申请成员', enterpriseRole: 'member' })
  const owner = await createAccountPage(browser, { id: 1, name: '企业所有者', enterpriseRole: 'owner' })
  const pageErrors: string[] = []
  member.page.on('pageerror', (error) => pageErrors.push(error.message))
  owner.page.on('pageerror', (error) => pageErrors.push(error.message))

  try {
    await member.page.getByLabel('远测目标').fill('https://authorized.example.com/')
    await member.page.getByLabel('资产授权依据').fill('已取得企业对该域名的明确书面授权')
    await member.page.getByLabel('范围和排除项').fill('仅检查网站根路径的安全响应头')
    await member.page.getByLabel('我已确认目标授权依据真实有效，并已阅读以上企业远测规则。').check()
    await member.page.getByRole('button', { name: '提交审批申请' }).click()
    await expect(member.page.getByText('待审批', { exact: true })).toBeVisible()
    await expect(owner.page.getByText('暂时没有远测申请')).toBeVisible()
    expect(shared.startCalls).toBe(0)
    expect(shared.targetRequests).toBe(0)

    await owner.page.getByRole('button', { name: '刷新', exact: true }).click()
    await expect(owner.page.getByRole('heading', { name: /authorized\.example\.com · 申请 #/ })).toBeVisible()
    await owner.page.getByRole('button', { name: '批准范围' }).click()
    await expect(owner.page.getByText('已批准 · 待申请人启动')).toBeVisible()
    expect(shared.startCalls).toBe(0)
    expect(shared.targetRequests).toBe(0)

    await member.page.getByRole('button', { name: '刷新', exact: true }).click()
    await expect(member.page.getByText('已批准 · 待申请人启动')).toBeVisible()
    await expect(member.page.getByRole('button', { name: '启动只读评估' })).toBeVisible()
    expect(shared.startCalls).toBe(0)
    await member.page.getByRole('button', { name: '启动只读评估' }).click()
    await expect.poll(() => shared.startCalls).toBe(1)
    await expect(member.page).toHaveURL(/\/remote-pentests\/synthetic-remote-task$/)
    await expect(member.page.getByRole('heading', { name: '合成远测浏览器验收' })).toBeVisible()
    expect(shared.targetRequests).toBe(0)
    expect(pageErrors).toEqual([])
  } finally {
    await owner.context.close()
    await member.context.close()
  }
})
