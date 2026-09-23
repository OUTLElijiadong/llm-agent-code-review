import { expect, test, type Page } from '@playwright/test'

// 仅用本地接口桩验证布局；生产账号和真实审查流程由另行的浏览器验收覆盖。
async function mockReviewStart(page: Page) {
  await page.addInitScript(() => localStorage.setItem('review_token', 'review-start-layout'))
  await page.route('**/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    let data: unknown = {}
    if (pathname === '/api/auth/me') data = { id: 7, username: '布局验收', role: 'user' }
    else if (pathname.endsWith('/roles')) data = [{ code: 'user' }]
    else if (pathname.endsWith('/permissions')) data = ['project:view', 'file:view', 'review:view', 'review:start']
    else if (pathname.endsWith('/menus')) data = []
    else if (pathname === '/api/projects') data = {
      items: [{ id: 15, project_name: '手工作坊管理系统', status: 'active', file_count: 1 }],
      total: 1, page: 1,
    }
    else if (pathname === '/api/code-files') data = {
      items: [{
        id: 783, project_id: 15,
        file_name: 'src/main/java/com/example/security/'.repeat(3) + 'SecurityConfig.java',
        language: 'Java', size_bytes: 3834, is_binary: 0, is_reviewable: true,
      }],
      total: 1, page: 1,
    }
    await route.fulfill({ json: { code: 0, message: 'ok', data } })
  })
}

for (const width of [320, 390, 768, 1024]) {
  test(`发起审查名称、文件与类型在 ${width}px 视口内完整显示`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 })
    await mockReviewStart(page)
    await page.goto('/reviews/start')
    await expect(page.getByPlaceholder('可选，输入任务名称')).toBeVisible()
    await page.locator('.review-scope-options .el-radio-button').nth(1).click()
    await expect(page.locator('.review-scope-options .el-radio-button').nth(1)).toHaveClass(/is-active/)
    await page.locator('.review-start-page .el-select').click()
    await page.getByRole('option', { name: '手工作坊管理系统' }).click()
    await expect(page.locator('.file-item')).toHaveCount(1)

    const layout = await page.locator('.review-start-page').evaluate((pageElement) => {
      const bounds = (selector: string) => pageElement.querySelector(selector)!.getBoundingClientRect()
      const card = bounds('.form-card')
      const input = bounds('.el-form-item:first-child .el-input')
      const label = bounds('.el-form-item:first-child .el-form-item__label')
      const fileList = bounds('.file-list')
      const fileContent = pageElement.querySelector('.file-list')!.closest('.el-form-item__content')!.getBoundingClientRect()
      const reviewType = bounds('.review-type-options')
      return {
        card: { left: card.left, right: card.right },
        input: { left: input.left, right: input.right, top: input.top },
        label: { bottom: label.bottom },
        fileList: { left: fileList.left, right: fileList.right },
        fileContent: { left: fileContent.left, right: fileContent.right },
        reviewType: { left: reviewType.left, right: reviewType.right },
        documentWidth: document.documentElement.scrollWidth,
      }
    })

    expect(layout.documentWidth).toBeLessThanOrEqual(width)
    expect(layout.card.left).toBeGreaterThanOrEqual(0)
    expect(layout.card.right).toBeLessThanOrEqual(width)
    expect(layout.input.left).toBeGreaterThanOrEqual(layout.card.left)
    expect(layout.input.right).toBeLessThanOrEqual(layout.card.right)
    expect(layout.fileList.left).toBeGreaterThanOrEqual(layout.fileContent.left)
    expect(layout.fileList.right).toBeLessThanOrEqual(layout.fileContent.right)
    expect(layout.reviewType.left).toBeGreaterThanOrEqual(layout.card.left)
    expect(layout.reviewType.right).toBeLessThanOrEqual(layout.card.right)
    if (width <= 768) {
      expect(layout.label.bottom).toBeLessThanOrEqual(layout.input.top)
      expect(layout.input.right - layout.input.left).toBeGreaterThan(180)
    }
  })
}
