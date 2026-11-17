import { expect, test, type Page } from '@playwright/test'

async function mockApi(page: Page, salesEnabled = false, role: 'admin' | 'user' | 'reviewer' = 'admin') {
  await page.addInitScript(() => localStorage.setItem('review_token', 'admin-billing-layout-fixture'))
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (!path.startsWith('/api/')) return route.continue()
    let data: unknown = []
    if (path === '/api/auth/me') data = { id: 7, username: '布局测试', role, status: 1 }
    else if (path.endsWith('/roles')) data = [{ id: 2, code: role }]
    else if (path.endsWith('/data-scope')) data = { scope: 'all' }
    else if (path === '/api/billing/pricing') data = {
      currency: 'CNY', pricing_version: 'fixture', credit_pack: { credits: 1000, price_fen: 400 },
      reviewer_month: { credits: 5000, price_fen: 4900 }, enterprise_seat_month: { credits: 5000, price_fen: 6900 },
      channels: { alipay: salesEnabled, wechat: salesEnabled }, sales_enabled: salesEnabled,
    }
    else if (path === '/api/billing/wallet') data = { available_milli: 1000000 }
    else if (path === '/api/billing/entitlement') data = { reviewer: false, enforcement_enabled: true }
    await route.fulfill({ json: { code: 0, message: 'ok', data } })
  })
}

test('管理员连续切换平台、充值和支持页面时小菱入口始终可见可打开', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await mockApi(page)
  await page.goto('/admin/platform?section=report-templates')
  expect(new URL(page.url()).searchParams.get('section')).toBe('report-templates')

  async function checkAssistant() {
    await expect(page.locator('.admin-layout')).toBeVisible()
    const trigger = page.locator('#admin-copilot-trigger-slot').getByRole('button', { name: '打开小菱 · 管理助手' })
    await expect(trigger).toBeVisible()
    await trigger.click()
    await expect(page.getByRole('dialog', { name: '管理副驾驶对话' })).toBeVisible()
    await page.getByRole('button', { name: '收起管理副驾驶' }).click()
    await expect(trigger).toBeVisible()
  }

  async function navigate(group: string, path: string) {
    const section = page.getByRole('button', { name: group, exact: true })
    if (await section.getAttribute('aria-expanded') === 'false') await section.click()
    await page.locator(`[data-route="${path}"]`).click()
    await expect.poll(() => new URL(page.url()).pathname).toBe(path)
  }

  await checkAssistant()
  await navigate('计费与对账', '/billing')
  await checkAssistant()
  await expect(page.getByRole('button', { name: '选择支付方式' })).toBeDisabled()
  await navigate('社区与支持', '/support')
  await checkAssistant()
  await navigate('平台治理', '/admin/platform')
  await checkAssistant()
})

for (const path of ['/billing/?enterprise_id=7', '/support/?section=feedback']) {
  test(`管理员带尾斜杠访问 ${path} 时保留页头小菱入口和参数`, async ({ page }) => {
    await mockApi(page)
    await page.goto(path)
    await expect(page.locator('.admin-layout')).toBeVisible()
    await expect(page.locator('#admin-copilot-trigger-slot .copilot-trigger')).toBeVisible()
    expect(new URL(page.url()).search).toBe(new URL(path, 'http://localhost').search)
  })
}

for (const role of ['user', 'reviewer'] as const) {
  test(`${role} 带尾斜杠访问充值页仍使用普通外壳`, async ({ page }) => {
    await mockApi(page, false, role)
    await page.goto('/billing/?enterprise_id=7')
    await expect(page.locator('.app-layout')).toBeVisible()
    await expect(page.locator('.admin-layout')).toHaveCount(0)
    expect(new URL(page.url()).searchParams.get('enterprise_id')).toBe('7')
  })
}

for (const width of [1440, 390]) {
  test(`${width}px 管理员充值页的真实样式显示小菱入口`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await mockApi(page)
    await page.goto('/billing?enterprise_id=7')
    await expect(page.locator('.admin-layout')).toBeVisible()
    const slot = page.locator('#admin-copilot-trigger-slot')
    await expect(slot).toBeVisible()
    const trigger = slot.getByRole('button', { name: '打开小菱 · 管理助手' })
    await expect(trigger).toBeVisible()
    await trigger.click()
    await expect(page.getByRole('dialog', { name: '管理副驾驶对话' })).toBeVisible()
    await expect(page.getByRole('button', { name: '选择支付方式' })).toBeDisabled()
    expect(new URL(page.url()).searchParams.get('enterprise_id')).toBe('7')
  })
}

test('测试数据开启渠道时管理员充值弹窗可以选择支付方式', async ({ page }) => {
  await mockApi(page, true)
  await page.goto('/billing/')
  await page.getByRole('button', { name: '选择支付方式' }).click()
  const dialog = page.getByRole('dialog', { name: '选择支付方式' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByLabel('支付宝')).toBeVisible()
  await dialog.getByLabel('微信支付').check()
  await expect(dialog.getByLabel('微信支付')).toBeChecked()
})
