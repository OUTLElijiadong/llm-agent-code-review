import { expect, test, type Page } from '@playwright/test'

const longName = 'src/main/java/com/example/security/'.repeat(3) + 'WorkshopAdminSecurityConfig.java'
const binaryName = 'workshop.db-shm'

async function mockSession(page: Page, options: { empty?: boolean; failOnce?: boolean; shrinkOnPageTwo?: boolean; onFileRequest?: (page: number) => void } = {}) {
  await page.addInitScript(() => localStorage.setItem('review_token', 'mobile-file-layout'))
  let failed = false
  let shrunk = false
  await page.route('**/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    let data: unknown = {}
    if (pathname === '/api/auth/me') data = { id: 7, username: '布局验收', role: 'user' }
    else if (pathname.endsWith('/roles')) data = [{ code: 'user' }]
    else if (pathname.endsWith('/permissions')) data = ['file:view', 'file:download', 'project:view']
    else if (pathname.endsWith('/menus')) data = []
    else if (pathname === '/api/code-files/784/download') {
      return route.fulfill({ body: 'SQLite fixture bytes', contentType: 'application/octet-stream' })
    }
    else if (pathname === '/api/code-files' && options.failOnce && !failed) {
      options.onFileRequest?.(Number(new URL(route.request().url()).searchParams.get('page') || '1'))
      failed = true
      return route.fulfill({ status: 503, json: { code: 50301, message: '读取暂不可用', data: null } })
    } else if (pathname === '/api/code-files') {
      const pageNumber = Number(new URL(route.request().url()).searchParams.get('page') || '1')
      options.onFileRequest?.(pageNumber)
      if (options.shrinkOnPageTwo && pageNumber === 2) {
        shrunk = true
        return route.fulfill({ json: { code: 0, message: 'ok', data: { items: [], total: 1, page: 2 } } })
      }
      const items = options.empty ? [] : pageNumber === 1 ? [
        { id: 783, project_id: 15, file_name: longName, language: 'Java', size_bytes: 3834, line_count: 73, version_no: 4, is_binary: 0, update_time: '2026-09-23T10:00:00Z' },
        { id: 784, project_id: 15, file_name: binaryName, language: '', size_bytes: 32768, line_count: 0, version_no: 2, is_binary: 1, update_time: '2026-09-23T10:01:00Z' },
      ] : [{ id: 785, project_id: 15, file_name: 'README.md', language: 'Markdown', size_bytes: 80, line_count: 4, version_no: 1, is_binary: 0, update_time: '2026-09-23T10:02:00Z' }]
      data = { items: shrunk ? items.slice(0, 1) : items, total: options.empty ? 0 : shrunk ? 1 : 12, page: pageNumber }
    }
    await route.fulfill({ json: { code: 0, message: 'ok', data } })
  })
}

for (const width of [320, 390]) {
  test(`手机 ${width}px 展示完整文件名、元数据与有权限的操作`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 })
    await mockSession(page)
    await page.goto('/code/15')
    await expect(page.locator('.mobile-file-card')).toHaveCount(2)
    await expect(page.locator('.desktop-file-table')).toBeHidden()

    const textCard = page.locator('.mobile-file-card').first()
    await expect(textCard.locator('.mobile-file-name')).toHaveText(longName)
    await expect(textCard.locator('.mobile-file-meta')).toContainText('Java')
    await expect(textCard.locator('.mobile-file-meta')).toContainText('文本')
    await expect(textCard.locator('.mobile-file-meta')).toContainText('3.7 KB')
    await expect(textCard.locator('.mobile-file-meta')).toContainText('v4')
    await expect(textCard.getByRole('button', { name: '查看代码' })).toBeVisible()
    await expect(textCard.getByRole('button', { name: '版本历史' })).toBeVisible()

    const binaryCard = page.locator('.mobile-file-card').nth(1)
    await expect(binaryCard.locator('.mobile-file-name')).toHaveText(binaryName)
    await expect(binaryCard.locator('.mobile-file-meta')).toContainText('二进制')
    await expect(binaryCard.getByRole('button', { name: '下载' })).toBeVisible()
    await expect(binaryCard.getByRole('button', { name: '查看元信息' })).toBeVisible()

    const geometry = await page.locator('.mobile-file-list').evaluate((list) => {
      const card = list.querySelector('.mobile-file-card')!.getBoundingClientRect()
      const name = list.querySelector('.mobile-file-name')!.getBoundingClientRect()
      const actions = list.querySelector('.mobile-file-actions')!.getBoundingClientRect()
      return { cardLeft: card.left, cardRight: card.right, nameRight: name.right, actionsRight: actions.right, documentWidth: document.documentElement.scrollWidth }
    })
    expect(geometry.cardLeft).toBeGreaterThanOrEqual(0)
    expect(geometry.cardRight).toBeLessThanOrEqual(width)
    expect(geometry.nameRight).toBeLessThanOrEqual(geometry.cardRight)
    expect(geometry.actionsRight).toBeLessThanOrEqual(geometry.cardRight)
    expect(geometry.documentWidth).toBeLessThanOrEqual(width)

    await textCard.getByRole('button', { name: '版本历史' }).click()
    await expect(page).toHaveURL(/\/code\/15\/file\/783\/versions$/)
  })
}

test('桌面仍使用完整表格；手机空态、错误重试和分页可用', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 844 })
  await mockSession(page)
  await page.goto('/code/15')
  await expect(page.locator('.desktop-file-table')).toBeVisible()
  await expect(page.locator('.mobile-file-list')).toBeHidden()

  const emptyPage = await page.context().newPage()
  await emptyPage.setViewportSize({ width: 390, height: 844 })
  await mockSession(emptyPage, { empty: true })
  await emptyPage.goto('/code/15')
  await expect(emptyPage.locator('.mobile-file-empty')).toHaveText('暂无代码文件')
  await emptyPage.close()

  const retryPage = await page.context().newPage()
  await retryPage.setViewportSize({ width: 390, height: 844 })
  let fileRequests = 0
  await mockSession(retryPage, { failOnce: true, onFileRequest: () => { fileRequests++ } })
  await retryPage.goto('/code/15')
  await expect(retryPage.getByTestId('file-load-error')).toBeVisible()
  await expect(retryPage.locator('.mobile-file-empty')).toHaveText('文件列表未读取成功')
  await retryPage.getByRole('button', { name: '重试读取' }).click()
  await expect(retryPage.locator('.mobile-file-card')).toHaveCount(2)
  expect(fileRequests).toBe(2)
  await retryPage.locator('.pagination-wrap .el-pagination').getByRole('button', { name: '下一页' }).click()
  await expect(retryPage.locator('.mobile-file-card')).toHaveCount(1)
  await expect(retryPage.locator('.mobile-file-name')).toHaveText('README.md')
  expect(fileRequests).toBe(3)
  await retryPage.close()
})

test('总数从多页缩至单页时只存在一个分页实例，自动纠正最多读取一次', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const requests: number[] = []
  await mockSession(page, { shrinkOnPageTwo: true, onFileRequest: (pageNumber) => requests.push(pageNumber) })
  await page.goto('/code/15')
  await expect(page.locator('.mobile-file-card')).toHaveCount(2)
  await expect(page.locator('.pagination-wrap .el-pagination')).toHaveCount(1)
  await page.locator('.pagination-wrap .el-pagination').getByRole('button', { name: '下一页' }).click()
  await expect(page.locator('.mobile-page-total')).toHaveText('共 1 个文件')
  await expect(page.locator('.pagination-wrap .el-pagination')).toHaveCount(1)
  // 1 页初读 + 2 页点击 + 单个分页组件自动纠正到 1 页，不出现双实例重复纠正。
  await expect.poll(() => requests).toEqual([1, 2, 1])
})

test('手机文件卡片的文本查看、二进制元信息和下载操作均可点击', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockSession(page)
  await page.goto('/code/15')
  await expect(page.locator('.mobile-file-card')).toHaveCount(2)

  await page.locator('.mobile-file-card').first().getByRole('button', { name: '查看代码' }).click()
  await expect(page).toHaveURL(/\/code\/15\/file\/783$/)
  await page.goto('/code/15')
  await expect(page.locator('.mobile-file-card')).toHaveCount(2)

  await page.locator('.mobile-file-card').nth(1).getByRole('button', { name: '查看元信息' }).click()
  await expect(page).toHaveURL(/\/code\/15\/file\/784$/)
  await page.goto('/code/15')
  await expect(page.locator('.mobile-file-card')).toHaveCount(2)

  const download = page.waitForEvent('download')
  await page.locator('.mobile-file-card').nth(1).getByRole('button', { name: '下载' }).click()
  expect((await download).suggestedFilename()).toBe(binaryName)
})
