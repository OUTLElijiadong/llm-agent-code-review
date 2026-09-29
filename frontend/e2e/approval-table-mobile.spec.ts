import { expect, test, type Page } from '@playwright/test'

async function mockAdminApprovals(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('review_token', 'approval-mobile-layout')
    localStorage.setItem('prism-page-guide-dismissed:admin:user-7', '1')
  })
  await page.route('**/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    let data: unknown = []
    if (pathname === '/api/auth/me') data = { id: 7, username: '审批布局验收', role: 'admin' }
    else if (pathname.endsWith('/roles')) data = [{ id: 2, code: 'admin' }]
    else if (pathname.endsWith('/permissions')) data = []
    else if (pathname.endsWith('/menus')) data = []
    else if (pathname.endsWith('/data-scope')) data = { scope: 'all' }
    else if (pathname === '/api/admin/approvals') data = [{
      id: 91,
      title: '移动端横向滚动与审批操作可达性验证事项',
      agent_code: 'security-reviewer',
      action: 'agent_package.publish',
      risk_level: 'high',
      status: 'pending',
    }]
    await route.fulfill({ json: { code: 0, message: 'ok', data } })
  })
}

for (const width of [390, 320]) {
  test(`审批列表在 ${width}px 真正横向滚动，键盘可到达操作列`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 })
    await mockAdminApprovals(page)
    await page.goto('/admin/governance?section=approvals&approvalType=execution')

    const scroller = page.getByRole('region', { name: '待办审批列表，可横向滚动查看全部列' })
    await expect(scroller).toBeVisible()
    const initial = await scroller.evaluate((element) => ({
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
      scrollLeft: element.scrollLeft,
    }))
    expect(initial.scrollWidth).toBeGreaterThan(initial.clientWidth)
    expect(initial.scrollLeft).toBe(0)

    await scroller.focus()
    for (let index = 0; index < 240; index += 1) {
      await page.keyboard.press('ArrowRight')
      const operationColumnReached = await scroller.evaluate((element) => {
        const operationHeader = [...element.querySelectorAll<HTMLElement>('th')]
          .find((header) => header.innerText.includes('操作'))
        const bounds = element.getBoundingClientRect()
        const operationBounds = operationHeader?.getBoundingClientRect()
        return Boolean(operationBounds
          && operationBounds.left >= bounds.left
          && operationBounds.right <= bounds.right)
      })
      if (operationColumnReached) break
    }

    const final = await scroller.evaluate((element) => {
      const operationHeader = [...element.querySelectorAll<HTMLElement>('th')]
        .find((header) => header.innerText.includes('操作'))
      const actionButton = element.querySelector<HTMLElement>('.el-table__body-wrapper tbody tr:first-child td:last-child button')
      const bounds = element.getBoundingClientRect()
      return {
        documentWidth: document.documentElement.scrollWidth,
        scrollLeft: element.scrollLeft,
        operationLeft: operationHeader?.getBoundingClientRect().left,
        operationRight: operationHeader?.getBoundingClientRect().right,
        actionButtonLeft: actionButton?.getBoundingClientRect().left,
        actionButtonRight: actionButton?.getBoundingClientRect().right,
        scrollerLeft: bounds.left,
        scrollerRight: bounds.right,
      }
    })
    expect(final.documentWidth).toBeLessThanOrEqual(width)
    expect(final.scrollLeft).toBeGreaterThan(0)
    expect(final.operationLeft).toBeGreaterThanOrEqual(final.scrollerLeft)
    expect(final.operationRight).toBeLessThanOrEqual(final.scrollerRight)
    expect(final.actionButtonLeft).toBeGreaterThanOrEqual(final.scrollerLeft)
    expect(final.actionButtonRight).toBeLessThanOrEqual(final.scrollerRight)
    await expect(scroller.getByRole('button', { name: '通过' })).toBeVisible()
  })
}

test('审批列表在 375px 触屏手势下可横向滚动到操作列', async ({ browser }) => {
  const context = await browser.newContext({
    baseURL: 'http://127.0.0.1:5173',
    viewport: { width: 375, height: 844 },
    isMobile: true,
    hasTouch: true,
  })
  const page = await context.newPage()
  await mockAdminApprovals(page)
  await page.goto('/admin/governance?section=approvals&approvalType=execution')

  const scroller = page.getByRole('region', { name: '待办审批列表，可横向滚动查看全部列' })
  await expect(scroller).toBeVisible()
  await expect(scroller).toHaveAttribute('aria-busy', 'false')
  const dimensions = await scroller.evaluate((element) => ({
    clientWidth: element.clientWidth,
    scrollWidth: element.scrollWidth,
  }))
  expect(dimensions.scrollWidth).toBeGreaterThan(dimensions.clientWidth)
  const bounds = await scroller.boundingBox()
  expect(bounds).not.toBeNull()
  const scrollHint = page.locator('.approval-scroll-hint')
  await expect(scrollHint).toBeVisible()
  const hintBounds = await scrollHint.boundingBox()
  expect(hintBounds).not.toBeNull()
  const touchPoint = {
    x: Math.round(bounds!.x + bounds!.width * 0.95),
    y: Math.round(hintBounds!.y + hintBounds!.height / 2),
  }
  const pointIsInsideScroller = () => scroller.evaluate((element, point) => {
    const target = document.elementFromPoint(point.x, point.y)
    return Boolean(target && (target === element || element.contains(target)))
  }, touchPoint)
  await expect.poll(pointIsInsideScroller, { timeout: 5_000 }).toBe(true)
  const cdp = await context.newCDPSession(page)
  // Playwright 的 Touchscreen API 只有 tap；用浏览器真实 touch 输入事件模拟一次完整横向拖动。
  await cdp.send('Input.dispatchTouchEvent', {
    type: 'touchStart', touchPoints: [{ ...touchPoint, id: 1 }],
  })
  for (let step = 1; step <= 12; step += 1) {
    await cdp.send('Input.dispatchTouchEvent', {
      type: 'touchMove',
      touchPoints: [{ x: touchPoint.x - step * 55, y: touchPoint.y, id: 1 }],
    })
    await page.waitForTimeout(16)
  }
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })

  const final = await scroller.evaluate((element) => {
    const operationHeader = [...element.querySelectorAll<HTMLElement>('th')]
      .find((header) => header.innerText.includes('操作'))
    const actionButton = element.querySelector<HTMLElement>('.el-table__body-wrapper tbody tr:first-child td:last-child button')
    const region = element.getBoundingClientRect()
    return {
      documentWidth: document.documentElement.scrollWidth,
      scrollLeft: element.scrollLeft,
      operationLeft: operationHeader?.getBoundingClientRect().left,
      operationRight: operationHeader?.getBoundingClientRect().right,
      actionButtonLeft: actionButton?.getBoundingClientRect().left,
      actionButtonRight: actionButton?.getBoundingClientRect().right,
      regionLeft: region.left,
      regionRight: region.right,
    }
  })
  expect(final.documentWidth).toBeLessThanOrEqual(375)
  expect(final.scrollLeft).toBeGreaterThan(0)
  expect(final.operationLeft).toBeGreaterThanOrEqual(final.regionLeft)
  expect(final.operationRight).toBeLessThanOrEqual(final.regionRight)
  expect(final.actionButtonLeft).toBeGreaterThanOrEqual(final.regionLeft)
  expect(final.actionButtonRight).toBeLessThanOrEqual(final.regionRight)
  await expect(scroller.getByRole('button', { name: '通过' })).toBeVisible()
  await cdp.detach()
  await context.close()
})
