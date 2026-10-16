import { expect, test, type Page } from '@playwright/test'

// 仅以接口桩验证布局和交互；真实生产审查结果需要另行通过真实接口验收。
const projects = [
  { id: 1, project_name: '皮卡丘漏洞平台', description: '用于布局回归的项目', language: 'PHP', file_count: 288 },
  { id: 2, project_name: '中文与 English 混排的较长项目标题', description: '说明文字较长时，卡片底部仍保持一致。'.repeat(5), language: 'PYTHON', file_count: 12 },
]
const issues = ['重复的样式规则块(DRY)', '公开函数缺少文档注释', '登录校验后缺少 exit 导致未授权访问及较长的中文标题'.repeat(3)].map((title, index) => ({
  id: index + 1, title, task_id: index + 10, task_name: '全量审查任务', project_name: projects[0].project_name,
  file_name: index === 2 ? 'very_long_path/'.repeat(12) + 'index.php' : 'index.php', line_number: 8,
  severity: index ? '高' : '中', status: 'unfixed', issue_type: '安全漏洞',
  description: `第 ${index + 1} 条问题的完整描述`, create_time: '2026-09-16T10:35:00',
}))
const reviewTasks = [{
  id: 21, task_name: '中文代码审查任务与英文 API_Review 混合标题', project_id: 1,
  project_name: projects[0].project_name, review_type: 'security', status: 'success',
  total_issues: 3, score: 86, duration_ms: 12000, create_time: '2026-09-16T10:35:00',
}]

async function mockSession(page: Page, canHandle = false, canCancelReview = false) {
  await page.addInitScript(() => localStorage.setItem('review_token', 'layout-fixture'))
  await page.route('**/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (!pathname.startsWith('/api/')) return route.continue()
    let data: unknown = {}
    if (pathname === '/api/auth/me') data = { id: 7, username: '布局验收', role: 'user' }
    else if (pathname.endsWith('/roles')) data = [{ code: 'user' }]
    else if (pathname.endsWith('/permissions')) data = [
      'project:view', 'file:view', 'issue:view', 'review:view',
      ...(canHandle ? ['issue:handle', 'issue:batch'] : []),
      ...(canCancelReview ? ['review:cancel'] : []),
    ]
    else if (pathname.endsWith('/menus')) data = []
    else if (pathname === '/api/projects') data = { items: projects, total: projects.length }
    else if (pathname === '/api/issues') data = { items: issues, total: issues.length }
    else if (pathname === '/api/review/tasks') data = { items: reviewTasks, total: reviewTasks.length }
    await route.fulfill({ json: { code: 0, message: 'ok', data } })
  })
}

for (const width of [1440, 768, 390, 320]) {
  test(`问题卡片操作列相对色带固定，权限差异不改变锚点：${width}px`, async ({ page }, testInfo) => {
    const readOnly = await page.context().newPage()
    await readOnly.setViewportSize({ width, height: 1000 })
    await mockSession(readOnly)
    await readOnly.goto('/issues')
    await expect(readOnly.locator('.issue-card')).toHaveCount(3)

    const canHandle = await page.context().newPage()
    await canHandle.setViewportSize({ width, height: 1000 })
    await mockSession(canHandle, true)
    await canHandle.goto('/issues')
    await expect(canHandle.locator('.issue-card')).toHaveCount(3)

    const offset = async (target: Page) => target.locator('.issue-card').first().evaluate((card) => {
      const band = card.querySelector('.ic-band')!.getBoundingClientRect()
      const details = card.querySelector('.ic-toggle')!.getBoundingClientRect()
      const task = [...card.querySelectorAll('button')].find((button) => button.textContent?.includes('查看任务'))!.getBoundingClientRect()
      return { detailOffset: details.x - band.x, taskOffset: task.x - band.x }
    })
    expect(await offset(canHandle)).toEqual(await offset(readOnly))
    expect(await readOnly.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
    expect(await canHandle.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
    if (width === 1440 || width === 390) {
      await readOnly.screenshot({ path: testInfo.outputPath(`issues-readonly-${width}.png`) })
      await canHandle.screenshot({ path: testInfo.outputPath(`issues-handle-${width}.png`) })
    }

    await readOnly.close()
    await canHandle.close()
  })
}

for (const width of [1440, 768, 390, 320]) {
  test(`审查任务卡片跨权限网格不跳列，中文标题与标签底边对齐：${width}px`, async ({ page }, testInfo) => {
    const geometries: Array<{ checkX: number; bandX: number; mainX: number; scoreX: number; titleBottom: number; typeTop: number; typeBottom: number; statusBottom: number }> = []
    for (const canCancel of [false, true]) {
      const target = canCancel ? await page.context().newPage() : page
      await target.setViewportSize({ width, height: 1000 })
      await mockSession(target, false, canCancel)
      await target.goto('/reviews')
      await expect(target.locator('.task-card')).toHaveCount(1)
      geometries.push(await target.locator('.task-card').first().evaluate((card) => {
        const rect = (selector: string) => card.querySelector(selector)!.getBoundingClientRect()
        return {
          checkX: rect('.tc-check-slot').x,
          bandX: rect('.tc-band').x,
          mainX: rect('.tc-main').x,
          scoreX: rect('.tc-score').x,
          titleBottom: rect('.tc-name').bottom,
          typeTop: rect('.tc-line1 .el-tag').top,
          typeBottom: rect('.tc-line1 .el-tag').bottom,
          statusBottom: rect('.tc-line1 .el-tag:last-child').bottom,
        }
      }))
      if (canCancel) await target.close()
    }
    for (const key of ['checkX', 'bandX', 'mainX', 'scoreX'] as const) {
      expect(geometries[1][key]).toBeCloseTo(geometries[0][key], 0)
    }
    for (const geometry of geometries) {
      expect(Math.abs(geometry.typeBottom - geometry.statusBottom)).toBeLessThanOrEqual(1)
      if (width > 760) expect(Math.abs(geometry.titleBottom - geometry.typeBottom)).toBeLessThanOrEqual(1)
      else expect(geometry.titleBottom).toBeLessThan(geometry.typeTop)
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
    if (width === 1440 || width === 390) await page.screenshot({ path: testInfo.outputPath(`review-tasks-${width}.png`) })
  })
}

async function assertInside(page: Page, parentSelector: string, childSelector: string) {
  const parent = await page.locator(parentSelector).boundingBox()
  const child = await page.locator(childSelector).boundingBox()
  expect(parent).not.toBeNull()
  expect(child).not.toBeNull()
  expect(child!.x).toBeGreaterThanOrEqual(parent!.x)
  expect(child!.x + child!.width).toBeLessThanOrEqual(parent!.x + parent!.width)
  expect(child!.y).toBeGreaterThanOrEqual(parent!.y)
  expect(child!.y + child!.height).toBeLessThanOrEqual(parent!.y + parent!.height)
}

test('侧栏收起、展开和刷新后按钮完整可点击，且不盖住品牌图标', async ({ page }, testInfo) => {
  await mockSession(page)
  await page.goto('/code')
  await expect(page.locator('.project-card')).toHaveCount(2)
  await assertInside(page, '.app-sidebar', '.sidebar-toggle')
  await page.getByRole('button', { name: '收起侧边栏', exact: true }).click()
  await expect(page.locator('.app-sidebar')).toHaveClass(/is-collapsed/)
  await expect.poll(async () => Math.round((await page.locator('.app-sidebar').boundingBox())!.width)).toBe(72)
  await assertInside(page, '.app-sidebar', '.sidebar-toggle')
  const mark = await page.locator('.sidebar-logo .prism-mark').boundingBox()
  const toggle = await page.locator('.sidebar-toggle').boundingBox()
  expect(toggle!.y).toBeGreaterThanOrEqual(mark!.y + mark!.height)
  await page.screenshot({ path: testInfo.outputPath('sidebar-collapsed.png') })
  await page.reload()
  await expect(page.locator('.app-sidebar')).toHaveClass(/is-collapsed/)
  await page.getByRole('button', { name: '展开侧边栏', exact: true }).click()
  await expect(page.locator('.app-sidebar')).not.toHaveClass(/is-collapsed/)
})

test('代码中心顶部仅保留主布局间距，搜索框与标题说明区底边对齐', async ({ page }, testInfo) => {
  await mockSession(page)
  await page.goto('/code')
  await expect(page.locator('.project-card')).toHaveCount(2)
  const geometry = await page.evaluate(() => {
    const main = document.querySelector('.app-layout-main')!
    const header = document.querySelector('.code-hub-page .page-header')!
    const subtitle = document.querySelector('.code-hub-page .page-sub')!.getBoundingClientRect()
    const search = document.querySelector('.code-hub-page .search-input')!.getBoundingClientRect()
    return { topGap: header.getBoundingClientRect().top - main.getBoundingClientRect().top, padding: parseFloat(getComputedStyle(main).paddingTop), bottomGap: subtitle.bottom - search.bottom }
  })
  expect(geometry.topGap).toBeCloseTo(geometry.padding, 0)
  expect(Math.abs(geometry.bottomGap)).toBeLessThanOrEqual(1)
  await testInfo.attach('header-geometry', { body: JSON.stringify(geometry), contentType: 'application/json' })
  await page.screenshot({ path: testInfo.outputPath('code-hub.png') })
  await page.getByPlaceholder('按项目名搜索').fill('English')
  await expect(page.locator('.project-card')).toHaveCount(1)
})

test('代码中心失败原因与重试按钮同时可见，重试后恢复项目卡片', async ({ page }) => {
  await mockSession(page)
  let failed = false
  await page.route(/\/api\/projects(?:\?|$)/, async (route) => {
    if (failed) return route.fallback()
    failed = true
    await route.fulfill({ status: 503, json: { code: 50301, message: '项目读取暂不可用', data: null } })
  })
  await page.goto('/code')
  await expect(page.locator('.code-hub-page .el-alert')).toContainText('项目读取暂不可用')
  await page.locator('.code-hub-page').getByRole('button', { name: '重新加载', exact: true }).click()
  await expect(page.locator('.project-card')).toHaveCount(2)
  await expect(page.locator('.code-hub-page .el-alert')).toHaveCount(0)
})

test('移动端沿用桌面收起状态时仍能打开完整导航并跳转关闭', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockSession(page)
  await page.addInitScript(() => localStorage.setItem('prism.sidebar.collapsed', '1'))
  await page.goto('/code')
  await expect(page.locator('.project-card')).toHaveCount(2)
  await page.getByRole('button', { name: '打开导航菜单' }).click()
  await expect(page.locator('.app-sidebar')).toHaveClass(/is-mobile-open/)
  await expect(page.locator('.sidebar-logo .logo-text')).toBeVisible()
  await expect(page.locator('.sidebar-toggle')).toBeHidden()
  await page.locator('.app-sidebar').getByRole('button', { name: '问题追踪', exact: true }).click()
  await expect(page).toHaveURL(/\/issues$/)
  await expect(page.locator('.app-sidebar')).not.toHaveClass(/is-mobile-open/)
  await expect(page.locator('.issue-card')).toHaveCount(3)
})

test('问题追踪请求失败有明确反馈，两类请求可独立点击重试', async ({ page }) => {
  await mockSession(page)
  let projectsFailed = false
  let issuesFailed = false
  await page.route(/\/api\/(projects|issues)(?:\?|$)/, async (route) => {
    const isProjects = new URL(route.request().url()).pathname === '/api/projects'
    if (isProjects ? projectsFailed : issuesFailed) return route.fallback()
    if (isProjects) projectsFailed = true
    else issuesFailed = true
    await route.fulfill({ status: 503, json: { code: 50301, message: isProjects ? '项目读取暂不可用' : '问题读取暂不可用', data: null } })
  })
  await page.goto('/issues')
  await expect(page.getByTestId('issue-project-load-error')).toContainText('项目读取暂不可用')
  await expect(page.getByTestId('issue-load-error')).toContainText('问题读取暂不可用')
  await expect(page.getByText('暂无问题', { exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: '重新加载问题', exact: true }).click()
  await expect(page.locator('.issue-card')).toHaveCount(3)
  await expect(page.getByTestId('issue-load-error')).toHaveCount(0)
  await expect(page.getByTestId('issue-project-load-error')).toBeVisible()
  await page.getByRole('button', { name: '重新加载项目', exact: true }).click()
  await expect(page.getByTestId('issue-project-load-error')).toHaveCount(0)
})

for (const canHandle of [false, true]) {
  for (const width of [1440, 1024, 768, 390, 320]) {
    test(`问题操作列保持对齐且窄屏无溢出：${width}px，${canHandle ? '可处理' : '只读'}权限`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 1000 })
      await mockSession(page, canHandle)
      await page.goto('/issues')
      await expect(page.locator('.issue-card')).toHaveCount(3)
      const geometry = await page.locator('.issue-card').evaluateAll((cards) => cards.map((card) => {
        const rect = card.getBoundingClientRect()
        const band = card.querySelector('.ic-band')!.getBoundingClientRect()
        const actions = card.querySelector('.ic-actions')!.getBoundingClientRect()
        const details = card.querySelector('.ic-toggle')!.getBoundingClientRect()
        const task = [...card.querySelectorAll('button')].find((button) => button.textContent?.includes('查看任务'))!.getBoundingClientRect()
        return { bandX: band.x, actionX: actions.x, detailX: details.x, taskX: task.x, actionRight: actions.right, right: rect.right, scroll: card.scrollWidth, client: card.clientWidth }
      }))
      for (const item of geometry) {
        expect(item.detailX).toBeCloseTo(geometry[0].detailX, 0)
        expect(item.taskX).toBeCloseTo(geometry[0].taskX, 0)
        expect(item.actionX - item.bandX).toBeCloseTo(geometry[0].actionX - geometry[0].bandX, 0)
        expect(item.actionRight).toBeLessThanOrEqual(item.right)
        expect(item.scroll).toBeLessThanOrEqual(item.client + 1)
      }
      await testInfo.attach('card-geometry', { body: JSON.stringify(geometry), contentType: 'application/json' })
      await page.locator('.ic-toggle').first().click()
      await expect(page.locator('.ic-desc').first()).toHaveText(/第 1 条问题的完整描述/)
      await expect(page.locator('.ic-toggle').first()).toHaveAttribute('aria-expanded', 'true')
      await expect(page.locator('.ic-check')).toHaveCount(canHandle ? 3 : 0)
      if (width === 1440 || width === 390) await page.screenshot({ path: testInfo.outputPath('issue-cards.png') })
      await page.locator('.issue-card').first().getByRole('button', { name: '查看任务', exact: true }).click()
      await expect(page).toHaveURL(/\/reviews\/10$/)
    })
  }
}
