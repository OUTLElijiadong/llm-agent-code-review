import { expect, test, type BrowserContext, type Page } from '@playwright/test'

const companyRoom = {
  id: 9, enterprise_id: 10, kind: 'company', group_id: null,
  participant_low_id: null, participant_high_id: null, create_time: null,
}
const groupRoom = {
  id: 20, enterprise_id: 10, kind: 'group', group_id: 22,
  participant_low_id: null, participant_high_id: null, create_time: null,
}

async function installApi(context: BrowserContext, page: Page): Promise<void> {
  await context.addInitScript(() => localStorage.setItem('review_token', 'enterprise-floating-chat-fixture'))
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const { pathname } = new URL(request.url())
    const method = request.method()
    if (!pathname.startsWith('/api/')) return route.continue()

    let data: unknown = []
    if (pathname === '/api/auth/me') {
      data = { id: 1, username: '悬浮窗验收成员', nickname: '成员', role: 'user', status: 1 }
    } else if (pathname === '/api/rbac/users/1/roles') {
      data = [{ code: 'user' }]
    } else if (pathname === '/api/rbac/users/1/permissions' || pathname === '/api/rbac/users/1/menus') {
      data = []
    } else if (pathname === '/api/rbac/users/1/data-scope') {
      data = null
    } else if (pathname === '/api/me/profile') {
      data = {
        user_id: 1, hobbies: '', goals: '', tech_stack: '', focus_areas: [],
        preferred_language: 'zh-CN', experience_level: 'beginner', auto_learn: false,
        derived_summary: '', derived_stats: {},
      }
    } else if (pathname === '/api/enterprises/mine') {
      data = [{ id: 10, name: '悬浮窗验收企业', status: 'active', role: 'member',
        default_group_id: null, subscription_status: 'active' }]
    } else if (pathname === '/api/enterprises/10/groups') {
      data = [{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'leader' }]
    } else if (pathname === '/api/enterprises/10/directory') {
      data = [{ user_id: 1, display_name: '成员' }, { user_id: 2, display_name: '王工' }]
    } else if (pathname === '/api/enterprises/10/conversations' && method === 'GET') {
      data = { items: [companyRoom], assistant_available: false }
    } else if (pathname === '/api/enterprises/10/conversations' && method === 'POST') {
      data = groupRoom
    } else if (pathname === '/api/enterprises/10/conversations/20/messages') {
      data = {
        items: [{ id: 1, conversation_id: 20, sender_id: 2, sender_type: 'human',
          client_message_id: 'floating-fixture-message', content: '只显示在当前小组会话的消息',
          create_time: '2026-11-07T08:00:00Z' }],
        assistant_available: false,
      }
    } else if (pathname === '/api/enterprises/10/conversations/20/assistant-runs') {
      data = { items: [], assistant_mode: 'enterprise_agent_runtime' }
    } else if (pathname === '/api/enterprises/10/announcements') {
      data = { items: [] }
    }

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ code: 0, message: 'ok', data }),
    })
  })
}

async function openGroupChat(page: Page): Promise<void> {
  await page.goto('/enterprise')
  await expect(page.getByRole('heading', { name: '企业空间' })).toBeVisible()
  await page.locator('.conversation-quick-actions button').filter({ hasText: '研发组' }).click()
  await expect(page.getByText('只显示在当前小组会话的消息')).toBeVisible()
}

async function floatingMetrics(page: Page) {
  return page.locator('.chat-pane.is-floating').evaluate((element) => {
    const rect = element.getBoundingClientRect()
    return {
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height,
      scrollWidth: element.scrollWidth,
      clientWidth: element.clientWidth,
      scrollHeight: element.scrollHeight,
      clientHeight: element.clientHeight,
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight,
      documentWidth: document.documentElement.scrollWidth,
    }
  })
}

test('桌面悬浮群聊跨企业功能页保留消息且可以收回', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await installApi(page.context(), page)
  await openGroupChat(page)

  await page.getByRole('button', { name: '弹出悬浮聊天窗口' }).click()
  const chat = page.locator('.chat-pane.is-floating')
  await expect(chat).toBeVisible()
  await expect(chat.getByText('只显示在当前小组会话的消息')).toBeVisible()
  const metrics = await floatingMetrics(page)
  expect(metrics.x).toBeGreaterThanOrEqual(0)
  expect(metrics.y).toBeGreaterThanOrEqual(0)
  expect(metrics.x + metrics.width).toBeLessThanOrEqual(metrics.viewportWidth)
  expect(metrics.y + metrics.height).toBeLessThanOrEqual(metrics.viewportHeight)

  const dragHandle = chat.locator('.chat-drag-handle')
  const dragBox = await dragHandle.boundingBox()
  expect(dragBox).not.toBeNull()
  await page.mouse.move(dragBox!.x + dragBox!.width / 2, dragBox!.y + dragBox!.height / 2)
  await page.mouse.down()
  await page.mouse.move(dragBox!.x + dragBox!.width / 2 - 80, dragBox!.y + dragBox!.height / 2 - 40, { steps: 4 })
  await page.mouse.up()
  const moved = await floatingMetrics(page)
  expect(moved.x).toBeLessThan(metrics.x)
  expect(moved.y).toBeLessThan(metrics.y)

  const resizeHandle = chat.locator('.panel-resize-handle[data-direction="se"]')
  const resizeBox = await resizeHandle.boundingBox()
  expect(resizeBox).not.toBeNull()
  await page.mouse.move(resizeBox!.x + resizeBox!.width / 2, resizeBox!.y + resizeBox!.height / 2)
  await page.mouse.down()
  await page.mouse.move(resizeBox!.x + resizeBox!.width / 2 + 40, resizeBox!.y + resizeBox!.height / 2 + 30, { steps: 4 })
  await page.mouse.up()
  const resized = await floatingMetrics(page)
  expect(resized.width).toBeGreaterThan(moved.width)
  expect(resized.height).toBeGreaterThan(moved.height)
  expect(resized.x + resized.width).toBeLessThanOrEqual(resized.viewportWidth)
  expect(resized.y + resized.height).toBeLessThanOrEqual(resized.viewportHeight)

  await page.getByRole('button', { name: '企业公告' }).click()
  await expect(page.getByRole('button', { name: '企业公告' })).toHaveAttribute('aria-pressed', 'true')
  await expect(chat).toBeVisible()
  await expect(chat.getByText('只显示在当前小组会话的消息')).toBeVisible()
  await chat.getByRole('button', { name: '收回聊天窗口' }).click()
  await expect(page.locator('.chat-pane.is-floating')).toHaveCount(0)
  expect(errors).toEqual([])
})

test('手机竖屏触控可打开并收起全屏小组聊天', async ({ browser, baseURL }) => {
  const context = await browser.newContext({
    baseURL,
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
    hasTouch: true,
    isMobile: true,
  })
  const page = await context.newPage()
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))

  try {
    await installApi(context, page)
    await openGroupChat(page)
    const button = page.getByRole('button', { name: '弹出悬浮聊天窗口' })
    const buttonBox = await button.boundingBox()
    expect(buttonBox).not.toBeNull()
    await page.touchscreen.tap(buttonBox!.x + buttonBox!.width / 2, buttonBox!.y + buttonBox!.height / 2)

    const chat = page.locator('.chat-pane.is-floating')
    await expect(chat).toBeVisible()
    const metrics = await floatingMetrics(page)
    expect(Math.round(metrics.x)).toBe(0)
    expect(Math.round(metrics.y)).toBe(0)
    expect(Math.round(metrics.width)).toBe(390)
    expect(Math.round(metrics.height)).toBe(844)
    expect(metrics.documentWidth).toBeLessThanOrEqual(metrics.viewportWidth)
    await expect(chat.getByText('只显示在当前小组会话的消息')).toBeVisible()

    const close = chat.getByRole('button', { name: '收回聊天窗口' })
    const closeBox = await close.boundingBox()
    expect(closeBox).not.toBeNull()
    await page.touchscreen.tap(closeBox!.x + closeBox!.width / 2, closeBox!.y + closeBox!.height / 2)
    await expect(page.locator('.chat-pane.is-floating')).toHaveCount(0)
    expect(errors).toEqual([])
  } finally {
    await context.close()
  }
})

test('手机横屏短视口保持全屏聊天且没有水平页面溢出', async ({ browser, baseURL }) => {
  const context = await browser.newContext({
    baseURL,
    viewport: { width: 844, height: 390 },
    deviceScaleFactor: 2,
    hasTouch: true,
    isMobile: true,
  })
  const page = await context.newPage()
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  try {
    await installApi(context, page)
    await openGroupChat(page)
    const open = page.getByRole('button', { name: '弹出悬浮聊天窗口' })
    const openBox = await open.boundingBox()
    expect(openBox).not.toBeNull()
    await page.touchscreen.tap(openBox!.x + openBox!.width / 2, openBox!.y + openBox!.height / 2)
    const metrics = await floatingMetrics(page)
    expect(Math.round(metrics.x)).toBe(0)
    expect(Math.round(metrics.y)).toBe(0)
    expect(Math.round(metrics.width)).toBe(844)
    expect(Math.round(metrics.height)).toBe(390)
    expect(metrics.documentWidth).toBeLessThanOrEqual(metrics.viewportWidth)
    await expect(page.locator('.chat-pane.is-floating').getByText('只显示在当前小组会话的消息')).toBeVisible()
    expect(errors).toEqual([])
  } finally {
    await context.close()
  }
})
